"""数据源 S3 读权限收敛：查询角色只读"平台日志桶 + 已登记的 ALB 数据源桶 + CloudTrail 桶"。
本模块有两部分：
- handler：独立的小 Lambda（SourceAccessFn），唯一权限是改写 api/worker 两个角色上名为 LogAuditSourceRead 的内联策略；
  只接受桶名列表，按固定模板生成只读策略，不接受任意策略文本。
- sync()：api 侧调用入口，汇总当前数据源的桶并调用上面的 Lambda。"""
import json, os, re, time

RE_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
POLICY_NAME = "LogAuditSourceRead"


def _policy(buckets, prefixes):
    stmts = [{"Sid": "ListSourceBuckets", "Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
              "Resource": [f"arn:aws:s3:::{b}" for b in buckets]},
             {"Sid": "ReadSourceObjects", "Effect": "Allow", "Action": ["s3:GetObject"],
              "Resource": sorted({f"arn:aws:s3:::{b}/{p}*" for b, p in prefixes})}]
    return json.dumps({"Version": "2012-10-17", "Statement": stmts})


def handler(event, _ctx):
    import boto3
    pairs = []
    for item in event.get("sources", []):
        b, p = item.get("bucket", ""), (item.get("prefix") or "").strip("/")
        if not RE_BUCKET.match(b) or not re.match(r"^[A-Za-z0-9!_.*'()/-]{0,200}$", p):
            raise ValueError(f"非法桶/前缀: {b}/{p}")
        pairs.append((b, (p + "/") if p else ""))
    buckets = sorted({b for b, _ in pairs})
    iam = boto3.client("iam")
    doc = _policy(buckets, pairs)
    for role in os.environ["TARGET_ROLES"].split(","):
        iam.put_role_policy(RoleName=role, PolicyName=POLICY_NAME, PolicyDocument=doc)
    print(json.dumps({"buckets": buckets, "statements": len(pairs)}))
    return {"buckets": buckets, "objects": [f"{b}/{p}" for b, p in pairs]}


def current_sources():
    from common import LOG_BUCKET, get, query
    src = [{"bucket": LOG_BUCKET, "prefix": ""}]
    for s in query("ALBSRC"):
        src.append({"bucket": s["bucket"], "prefix": s.get("prefix", "")})
    ct = get("SETTINGS", "cloudtrail")
    if ct:
        src.append({"bucket": ct["bucket"], "prefix": ct.get("prefix", "")})
    return src


def sync(extra=None, wait_bucket=None):
    """按当前数据源（+ extra）重写读权限。wait_bucket：等待 IAM 生效（最多约 30 秒）后再返回。"""
    from common import client
    fn = os.environ.get("SOURCE_ACCESS_FN")
    if not fn:
        return {"skipped": True}
    sources = current_sources() + (extra or [])
    r = client("lambda").invoke(FunctionName=fn, Payload=json.dumps({"sources": sources}).encode())
    out = json.loads(r["Payload"].read() or b"{}")
    if r.get("FunctionError"):
        raise RuntimeError(f"更新数据源读权限失败: {out}")
    if wait_bucket:
        s3 = client("s3")
        for i in range(15):
            try:
                s3.list_objects_v2(Bucket=wait_bucket["bucket"], Prefix=wait_bucket.get("prefix", ""), MaxKeys=1)
                break
            except Exception:  # noqa  IAM 最终一致，等待生效
                time.sleep(2)
    return out
