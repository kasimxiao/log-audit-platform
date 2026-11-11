#!/usr/bin/env python3
"""主机日志 JSON → Parquet 三级分区迁移（主库 hostlogs/ 与评估库 eval/hostlogs/），同时补 fields。
旧 JSON 文件移到 <prefix>-json-archive/。幂等：已迁移的 JSON 不再存在于原位置。"""
import gzip, io, json, os, sys, tempfile
import boto3
REGION = os.environ.get("LA_REGION", "ap-northeast-1"); os.environ["AWS_REGION"] = REGION
cfn = boto3.client("cloudformation", region_name=REGION)
P = {o["OutputKey"]: o["OutputValue"] for o in cfn.describe_stacks(StackName="LogAuditPlatform")["Stacks"][0]["Outputs"]}
os.environ.update(TABLE_NAME=P["TableName"], LOG_BUCKET=P["LogBucketName"], ATHENA_WORKGROUP="logaudit")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "app"))
import athena_svc, extractor, parsers
LB = P["LogBucketName"]; s3 = boto3.client("s3", region_name=REGION)

def migrate(db, prefix):
    athena_svc.run_sync(f"DROP TABLE IF EXISTS `{db}`.`hostlogs`", db=db)
    athena_svc.run_sync(athena_svc.hostlogs_ddl(db, f"s3://{LB}/{prefix}"), db=db)
    extractor._table_sd.cache_clear()
    keys = [o["Key"] for pg in s3.get_paginator("list_objects_v2").paginate(Bucket=LB, Prefix=prefix)
            for o in pg.get("Contents", []) if o["Key"].endswith(".json.gz")]
    arch = prefix.rstrip("/") + "-json-archive/"
    total = 0
    for k in keys:
        rows = [json.loads(l) for l in gzip.decompress(s3.get_object(Bucket=LB, Key=k)["Body"].read()).decode().splitlines() if l.strip()]
        if not rows: continue
        dt = k.split("dt=")[1][:10]
        groups = {}
        for r in rows: groups.setdefault((r["instance_id"], r["sync_id"], r["region"]), []).append(r)
        for (iid, sid, region), rs in groups.items():
            with tempfile.TemporaryDirectory() as td:
                W = extractor.Writers(td, db, LB, prefix, dt, region, iid, sid)
                for r in rs:
                    W.add(r["logtype"], {"region": region, "host_name": r["host_name"], "path": r["path"], "line_no": r["line_no"],
                                         "event_time": r.get("event_time"), "line": r["line"], "sync_id": sid,
                                         "collected_at": r["collected_at"], "fields": parsers.parse_fields(r["logtype"], r["line"])})
                total += sum(W.close().values())
        s3.copy_object(Bucket=LB, Key=arch + k[len(prefix):], CopySource={"Bucket": LB, "Key": k}); s3.delete_object(Bucket=LB, Key=k)
    print(f"{db}: {len(keys)} 个 JSON 文件 → {total} 行 Parquet")

if __name__ == "__main__":
    if "--rebuild-eval" in sys.argv:  # 从归档的 JSON 重新生成评估库（解析规则变更后用）
        for pg in s3.get_paginator("list_objects_v2").paginate(Bucket=LB, Prefix="eval/hostlogs/"):
            for o in pg.get("Contents", []): s3.delete_object(Bucket=LB, Key=o["Key"])
        for pg in s3.get_paginator("list_objects_v2").paginate(Bucket=LB, Prefix="eval/hostlogs-json-archive/"):
            for o in pg.get("Contents", []):
                s3.copy_object(Bucket=LB, Key="eval/hostlogs/" + o["Key"][len("eval/hostlogs-json-archive/"):], CopySource={"Bucket": LB, "Key": o["Key"]})
        migrate("logaudit_eval", "eval/hostlogs/"); athena_svc.ensure_views("logaudit_eval")
    else:
        migrate("logaudit_eval", "eval/hostlogs/"); migrate("logaudit", "hostlogs/")
        athena_svc.ensure_views("logaudit_eval"); athena_svc.ensure_views("logaudit")
