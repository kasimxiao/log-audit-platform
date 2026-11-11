"""CloudTrail：配置 trail 桶建表 + 预置审计查询。"""
import re

from common import ACCOUNT_ID, ApiError, GLUE_DB, client, get, now_iso, put, sql_str
import athena_svc
from hosts import enabled_regions


def list_trails():
    out = []
    for t in client("cloudtrail").describe_trails(includeShadowTrails=True)["trailList"]:
        out.append({"name": t["Name"], "bucket": t.get("S3BucketName"), "prefix": t.get("S3KeyPrefix") or "",
                    "home_region": t.get("HomeRegion"), "multi_region": t.get("IsMultiRegionTrail")})
    return out


def config():
    return get("SETTINGS", "cloudtrail")


def configure(body, actor):
    bucket, prefix = body.get("bucket", ""), body.get("prefix", "") or ""
    if not re.match(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", bucket) or not re.match(r"^[A-Za-z0-9_./-]{0,200}$", prefix):
        raise ApiError(400, "桶名/前缀非法")
    regions = enabled_regions()
    import source_access
    source_access.sync(extra=[{"bucket": bucket, "prefix": prefix}])
    athena_svc.run_sync(f"DROP TABLE IF EXISTS `{GLUE_DB}`.`cloudtrail`")
    athena_svc.run_sync(athena_svc.cloudtrail_ddl(GLUE_DB, bucket, prefix, ACCOUNT_ID, regions))
    item = {"bucket": bucket, "prefix": prefix, "regions": regions, "updated_by": actor, "updated_at": now_iso()}
    put("SETTINGS", "cloudtrail", **item)
    source_access.sync()
    return item


PRESETS = {
    "console_login": ("控制台登录（含失败）", """SELECT eventtime, useridentity.type AS id_type, coalesce(useridentity.username, useridentity.arn) AS who,
  sourceipaddress, json_extract_scalar(responseelements, '$.ConsoleLogin') AS result,
  json_extract_scalar(additionaleventdata, '$.MFAUsed') AS mfa FROM cloudtrail WHERE {w} AND eventname = 'ConsoleLogin'
ORDER BY eventtime DESC LIMIT 500"""),
    "root_activity": ("root 账号活动", """SELECT eventtime, eventsource, eventname, awsregion, sourceipaddress, errorcode FROM cloudtrail
WHERE {w} AND useridentity.type = 'Root' ORDER BY eventtime DESC LIMIT 500"""),
    "iam_changes": ("IAM 变更", """SELECT eventtime, eventname, useridentity.arn AS who, sourceipaddress, errorcode,
  substr(requestparameters, 1, 300) AS params FROM cloudtrail WHERE {w} AND eventsource = 'iam.amazonaws.com'
  AND regexp_like(eventname, '^(Create|Delete|Put|Attach|Detach|Update|Add|Remove)') ORDER BY eventtime DESC LIMIT 500"""),
    "sg_changes": ("安全组/网络 ACL 变更", """SELECT eventtime, awsregion, eventname, useridentity.arn AS who, sourceipaddress,
  substr(requestparameters, 1, 400) AS params FROM cloudtrail WHERE {w} AND eventsource = 'ec2.amazonaws.com'
  AND eventname IN ('AuthorizeSecurityGroupIngress','AuthorizeSecurityGroupEgress','RevokeSecurityGroupIngress','RevokeSecurityGroupEgress',
  'CreateSecurityGroup','DeleteSecurityGroup','ModifySecurityGroupRules','CreateNetworkAclEntry','ReplaceNetworkAclEntry','DeleteNetworkAclEntry')
ORDER BY eventtime DESC LIMIT 500"""),
    "destructive": ("删除/终止类操作", """SELECT eventtime, awsregion, eventsource, eventname, useridentity.arn AS who, sourceipaddress, errorcode
FROM cloudtrail WHERE {w} AND regexp_like(eventname, '^(Delete|Terminate|Remove|Disable|Stop)') AND readonly = 'false'
ORDER BY eventtime DESC LIMIT 500"""),
    "access_denied": ("权限拒绝（AccessDenied）", """SELECT useridentity.arn AS who, eventsource, eventname, errorcode, count(*) AS n,
  max(eventtime) AS last_seen FROM cloudtrail WHERE {w} AND (errorcode LIKE '%AccessDenied%' OR errorcode LIKE '%Unauthorized%')
GROUP BY 1, 2, 3, 4 ORDER BY n DESC LIMIT 200"""),
    "ssm_sessions": ("SSM 会话/远程命令", """SELECT eventtime, awsregion, eventname, useridentity.arn AS who, sourceipaddress,
  json_extract_scalar(requestparameters, '$.target') AS target, json_extract_scalar(requestparameters, '$.documentName') AS doc
FROM cloudtrail WHERE {w} AND eventsource = 'ssm.amazonaws.com' AND eventname IN ('StartSession','SendCommand','ResumeSession')
ORDER BY eventtime DESC LIMIT 500"""),
    "top_callers": ("调用方 Top（按 API 量）", """SELECT coalesce(useridentity.arn, useridentity.invokedby) AS who, count(*) AS n,
  count_if(errorcode IS NOT NULL) AS errors, count(DISTINCT eventname) AS apis FROM cloudtrail WHERE {w}
GROUP BY 1 ORDER BY n DESC LIMIT 50"""),
}


def where(f):
    d1, d2 = f.get("date_from"), f.get("date_to") or f.get("date_from")
    if not (d1 and re.match(r"^\d{4}-\d{2}-\d{2}$", d1) and re.match(r"^\d{4}-\d{2}-\d{2}$", d2)):
        raise ApiError(400, "日期格式 YYYY-MM-DD")
    regs = f.get("regions") or ["us-east-1"]
    for r in regs:
        if not re.match(r"^[a-z]{2}(-[a-z]+)+-\d$", r):
            raise ApiError(400, "区域非法")
    w = [f"day BETWEEN {sql_str(d1.replace('-', '/'))} AND {sql_str(d2.replace('-', '/'))}",
         "region IN (" + ",".join(sql_str(r) for r in regs[:30]) + ")"]
    if f.get("who"):
        w.append(f"strpos(coalesce(useridentity.arn, ''), {sql_str(f['who'][:200])}) > 0")
    if f.get("event_name"):
        w.append(f"eventname = {sql_str(f['event_name'][:100])}")
    if f.get("source_ip"):
        w.append(f"sourceipaddress = {sql_str(f['source_ip'][:64])}")
    return " AND ".join(w)


def preset_sql(pid, f):
    if not config():
        raise ApiError(400, "尚未配置 CloudTrail 数据源")
    if pid not in PRESETS:
        raise ApiError(404, "预置查询不存在")
    return PRESETS[pid][1].replace("{w}", where(f))


def search_sql(f):
    if not config():
        raise ApiError(400, "尚未配置 CloudTrail 数据源")
    lim = max(1, min(int(f.get("limit") or 200), 2000))
    return (f"SELECT eventtime, awsregion, eventsource, eventname, useridentity.arn AS who, sourceipaddress, errorcode, "
            f"substr(requestparameters, 1, 300) AS params FROM cloudtrail WHERE {where(f)} ORDER BY eventtime DESC LIMIT {lim}")
