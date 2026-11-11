#!/usr/bin/env python3
"""部署后初始化（幂等）：上传合成数据、建 Athena 表、注册 ALB/CloudTrail 数据源、默认模板、管理员账号。
用法：[AWS_PROFILE=<你的 profile>] [LA_REGION=<区域>] python3 scripts/bootstrap.py
复用 backend/app 模块（与线上同一套建表逻辑）。"""
import json, os, secrets, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGION = os.environ.get("LA_REGION", "ap-northeast-1")
os.environ["AWS_REGION"] = REGION
import boto3  # noqa

cfn = boto3.client("cloudformation", region_name=REGION)
out = lambda st: {o["OutputKey"]: o["OutputValue"] for o in cfn.describe_stacks(StackName=st)["Stacks"][0].get("Outputs", [])}
P = out("LogAuditPlatform")
try:
    T = out("LogAuditTestEnv")
except cfn.exceptions.ClientError:  # 未部署测试环境（cdk 未传 -c vpcId）
    T = {}
    print("未找到 LogAuditTestEnv 栈，跳过测试环境 ALB 数据源")
ACCOUNT = boto3.client("sts").get_caller_identity()["Account"]
os.environ.update(TABLE_NAME=P["TableName"], LOG_BUCKET=P["LogBucketName"], USER_POOL_ID=P["UserPoolId"], ACCOUNT_ID=ACCOUNT,
                  SOURCE_ACCESS_FN=P.get("SourceAccessFnName", ""),
                  GLUE_DB="logaudit", EVAL_DB="logaudit_eval", ATHENA_WORKGROUP="logaudit")
sys.path.insert(0, os.path.join(ROOT, "backend", "app"))
import alb, athena_svc, cloudtrail, syncsvc, users  # noqa
from common import get  # noqa

LB = P["LogBucketName"]
sh = lambda *a: subprocess.run(list(a), check=True)

print("1) 合成数据 → S3")
subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "gen_synthetic.py"), ACCOUNT, REGION], check=True, stdout=subprocess.DEVNULL)
sh("aws", "s3", "sync", "--quiet", os.path.join(ROOT, "data/synthetic/hostlogs"), f"s3://{LB}/eval/hostlogs-json-archive/", "--region", REGION)
sh("aws", "s3", "sync", "--quiet", os.path.join(ROOT, "data/synthetic/alb"), f"s3://{LB}/synthetic/alb/", "--region", REGION)

print("2) Athena 表：主库 hostlogs（Parquet 三级分区）+ 视图；评估库由合成 JSON 转 Parquet；alb_demo")
athena_svc.ensure_base_tables("logaudit", "hostlogs/")
subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "migrate_hostlogs_parquet.py"), "--rebuild-eval"], check=True)
athena_svc.run_sync(athena_svc.alb_ddl("logaudit_eval", "alb_demo", LB, "synthetic/alb", ACCOUNT, REGION, "2026/11/01"), db="logaudit_eval")

print("3) ALB 数据源")
for sid, name, bucket, prefix in [("demo", "合成演示数据（shop-web/shop-api）", LB, "synthetic/alb"),
                                  ("shop_web", "测试环境 la-shop-web（桶 A）", T.get("AlbLogsBucketA"), ""),
                                  ("shop_api", "测试环境 la-shop-api（桶 B / prod 前缀）", T.get("AlbLogsBucketB"), "prod")]:
    if not bucket:
        continue
    if get("ALBSRC", sid):
        print("   已存在", sid); continue
    alb.create_source({"id": sid, "name": name, "bucket": bucket, "prefix": prefix, "region": REGION, "account": ACCOUNT}, "bootstrap")
    print("   +", sid)

print("4) CloudTrail 数据源")
if not cloudtrail.config():
    trails = [t for t in cloudtrail.list_trails() if t["multi_region"]]
    if trails:
        cloudtrail.configure({"bucket": trails[0]["bucket"], "prefix": trails[0]["prefix"]}, "bootstrap")
        print("   使用 trail", trails[0]["name"])

print("4b) 数据源读权限（按已登记的桶收敛）")
import source_access  # noqa
print("   ", source_access.sync())

print("5) 默认采集模板")
syncsvc.list_templates()

print("6) 管理员账号")
pw_file = os.path.expanduser("~/.log-audit-admin.txt")
cog = boto3.client("cognito-idp", region_name=REGION)
try:
    cog.admin_get_user(UserPoolId=P["UserPoolId"], Username="admin")
    print("   admin 已存在（密码见", pw_file, "）")
except cog.exceptions.UserNotFoundException:
    pw = "La-" + secrets.token_urlsafe(10) + "9a"
    cog.admin_create_user(UserPoolId=P["UserPoolId"], Username="admin", MessageAction="SUPPRESS",
                          UserAttributes=[{"Name": "name", "Value": "平台管理员"}])
    cog.admin_set_user_password(UserPoolId=P["UserPoolId"], Username="admin", Password=pw, Permanent=True)
    cog.admin_add_user_to_group(UserPoolId=P["UserPoolId"], Username="admin", GroupName="admin")
    with open(pw_file, "w") as f:
        f.write(f"url={P['Url']}\nusername=admin\npassword={pw}\n")
    os.chmod(pw_file, 0o600)
    print("   已创建，凭证写入", pw_file)

print("7) 前端发布")
with open(os.path.join(ROOT, "web", "config.js"), "w") as f:
    f.write("window.LA_CONFIG = " + json.dumps({"region": REGION, "userPoolId": P["UserPoolId"], "clientId": P["UserPoolClientId"]}) + ";\n")
sh("aws", "s3", "sync", "--quiet", "--delete", os.path.join(ROOT, "web"), f"s3://{P['WebBucketName']}/", "--region", REGION)
sh("aws", "cloudfront", "create-invalidation", "--distribution-id", P["DistributionId"], "--paths", "/*", "--query", "Invalidation.Id", "--output", "text")
print("完成：", P["Url"])
