#!/usr/bin/env python3
"""用当前 ALB_REGEX 重建所有 ALB 表（主库各数据源 + 评估库 alb_demo）。只删重建 Glue 表定义，不动 S3 日志。"""
import os, sys
import boto3
REGION = os.environ.get("LA_REGION", "ap-northeast-1"); os.environ["AWS_REGION"] = REGION
cfn = boto3.client("cloudformation", region_name=REGION)
P = {o["OutputKey"]: o["OutputValue"] for o in cfn.describe_stacks(StackName="LogAuditPlatform")["Stacks"][0]["Outputs"]}
os.environ.update(TABLE_NAME=P["TableName"], LOG_BUCKET=P["LogBucketName"], ATHENA_WORKGROUP="logaudit")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "app"))
import athena_svc
from common import query
ACC = boto3.client("sts").get_caller_identity()["Account"]
for s in query("ALBSRC"):
    athena_svc.run_sync(f"DROP TABLE IF EXISTS `logaudit`.`{s['table']}`")
    athena_svc.run_sync(athena_svc.alb_ddl("logaudit", s["table"], s["bucket"], s["prefix"], s["account"], s["region"]))
    print("重建", s["table"])
athena_svc.run_sync("DROP TABLE IF EXISTS `logaudit_eval`.`alb_demo`", db="logaudit_eval")
athena_svc.run_sync(athena_svc.alb_ddl("logaudit_eval", "alb_demo", P["LogBucketName"], "synthetic/alb", ACC, REGION, "2026/11/01"), db="logaudit_eval")
print("重建 logaudit_eval.alb_demo")
