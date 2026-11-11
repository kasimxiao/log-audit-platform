#!/usr/bin/env python3
"""在指定主机上运行当前版本采集脚本，上传到 collector-test/（不触发入库），下载回来展示包内容。
用法：python3 scripts/test_collector.py <instance_id> "<kind|binmode|logtype|path>" ... [--shell "预处理命令"]"""
import io, json, os, sys, tarfile, time
import boto3
REGION = os.environ.get("LA_REGION", "ap-northeast-1"); os.environ["AWS_REGION"] = REGION
cfn = boto3.client("cloudformation", region_name=REGION)
P = {o["OutputKey"]: o["OutputValue"] for o in cfn.describe_stacks(StackName="LogAuditPlatform")["Stacks"][0]["Outputs"]}
os.environ.update(LOG_BUCKET=P["LogBucketName"], TABLE_NAME=P["TableName"])
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "app"))
import syncsvc
args = sys.argv[1:]; iid = args.pop(0); pre = ""
if "--shell" in args: i = args.index("--shell"); pre = args[i + 1]; del args[i:i + 2]
paths = [dict(zip(("kind", "binmode", "logtype", "path"), a.split("|", 3))) for a in args]
key = f"collector-test/{iid}/{int(time.time())}.tgz"
url = syncsvc._presigner().generate_presigned_url("put_object", Params={"Bucket": P["LogBucketName"], "Key": key}, ExpiresIn=900)
ssm = boto3.client("ssm", region_name=REGION)
cid = ssm.send_command(InstanceIds=[iid], DocumentName="AWS-RunShellScript",
                       Parameters={"commands": [syncsvc.render_script(paths, url, "test-" + str(int(time.time()))).replace("set -u\n", "set -u\n" + pre + "\n", 1)]})["Command"]["CommandId"]
for _ in range(60):
    time.sleep(2)
    try: inv = ssm.get_command_invocation(CommandId=cid, InstanceId=iid)
    except ssm.exceptions.InvocationDoesNotExist: continue
    if inv["Status"] not in ("Pending", "InProgress", "Delayed"): break
print("host:", inv["Status"], inv["StandardOutputContent"].strip()[-300:], inv["StandardErrorContent"].strip()[-300:])
body = boto3.client("s3", region_name=REGION).get_object(Bucket=P["LogBucketName"], Key=key)["Body"].read()
tf = tarfile.open(fileobj=io.BytesIO(body), mode="r:gz")
man = json.load(tf.extractfile("./manifest.json"))
print("manifest tz:", man.get("tz"), man.get("utc_offset"), "os:", man.get("os"))
for e in man["entries"]:
    print(f"  {e['kind']:8s} {e['path']:32s} {e['status']:8s} bytes={e['bytes']:<8} member={e['member']} note={e['note']}")
    if e["member"] and not e["member"].startswith("raw/"):
        data = tf.extractfile("./" + e["member"]).read()
        if e["member"].startswith("utmp/"):
            import extractor
            sz = int(man.get("utmp_size") or 384); recs = list(extractor.decode_utmp(data, e["logtype"], sz))
            print(f"      [二进制 {len(data)} 字节 / 记录 {sz} 字节 = {len(data)//sz} 条，解码 {len(recs)} 条]")
            for l, t, f in recs[:2] + recs[-1:]: print("      |", l[:110], "|", t, f.get("action"), f.get("user"), f.get("src_ip"))
        else:
            for l in data.decode("utf-8", "replace").splitlines()[:3]: print("      |", l[:150])
