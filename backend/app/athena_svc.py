"""Athena：异步查询（带归属）、同步等待、建表 DDL（hostlogs / alb_* / cloudtrail）。"""
import re, time

from common import ACCOUNT_ID, ApiError, GLUE_DB, HOME_REGION, LOG_BUCKET, WORKGROUP, client, get, now_iso, put


def start(sql, db=GLUE_DB, owner=None, label=""):
    r = client("athena").start_query_execution(QueryString=sql, QueryExecutionContext={"Database": db}, WorkGroup=WORKGROUP)
    qid = r["QueryExecutionId"]
    if owner:
        put("QUERY", qid, owner=owner, label=label[:200], sql=sql[:8000], db=db, created_at=now_iso())
    return qid


def status(qid):
    q = client("athena").get_query_execution(QueryExecutionId=qid)["QueryExecution"]
    st = q["Status"]
    return {"state": st["State"], "reason": st.get("StateChangeReason"),
            "bytes_scanned": q.get("Statistics", {}).get("DataScannedInBytes", 0),
            "elapsed_ms": q.get("Statistics", {}).get("TotalExecutionTimeInMillis", 0)}


def results(qid, max_rows=500):
    pg = client("athena").get_paginator("get_query_results")
    cols, rows = None, []
    for page in pg.paginate(QueryExecutionId=qid, PaginationConfig={"PageSize": 1000}):
        rs = page["ResultSet"]
        if cols is None:
            cols = [c["Name"] for c in rs["ResultSetMetadata"]["ColumnInfo"]]
            data = rs["Rows"][1:]  # 首行为表头
        else:
            data = rs["Rows"]
        for r in data:
            rows.append([d.get("VarCharValue") for d in r["Data"]])
            if len(rows) > max_rows:
                return cols or [], rows[:max_rows], True
    return cols or [], rows, False


def get_owned(qid, actor, is_admin, max_rows=500):
    rec = get("QUERY", qid)
    if not rec or (rec["owner"] != actor and not is_admin):
        raise ApiError(404, "查询不存在")
    s = status(qid)
    out = {"query_id": qid, **s, "sql": rec.get("sql"), "label": rec.get("label")}
    if s["state"] == "SUCCEEDED":
        out["columns"], out["rows"], out["truncated"] = results(qid, max_rows)
    return out


def run_sync(sql, db=GLUE_DB, timeout=120, max_rows=500):
    qid = start(sql, db)
    t0 = time.time()
    while True:
        s = status(qid)
        if s["state"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        if time.time() - t0 > timeout:
            client("athena").stop_query_execution(QueryExecutionId=qid)
            raise ApiError(504, "查询超时")
        time.sleep(0.6)
    if s["state"] != "SUCCEEDED":
        raise ApiError(400, f"Athena 查询失败: {s.get('reason')}", "athena_failed")
    cols, rows, tr = results(qid, max_rows)
    return {"query_id": qid, "columns": cols, "rows": rows, "truncated": tr, **s}


# ------------------------------------------------------------------ DDL
def hostlogs_ddl(db, location):
    """主机日志表：Parquet，按 同步日期/实例/日志类型 三级分区（分区由入库 Lambda 登记到 Glue）。
    fields 为入库时预解析的常用字段（src_ip/user/action/result/command/status/url...）。"""
    return f"""CREATE EXTERNAL TABLE IF NOT EXISTS `{db}`.`hostlogs` (
  region string, host_name string, path string, line_no bigint, event_time string, line string,
  sync_id string, collected_at string, fields map<string,string>)
PARTITIONED BY (dt string, instance_id string, logtype string)
STORED AS PARQUET
LOCATION '{location}'
TBLPROPERTIES ('parquet.compression'='SNAPPY')"""


HOSTLOG_VIEWS = {
    "v_auth": ("认证/授权（SSH、sudo、账号变更）", "logtype = 'auth'",
               ["action", "result", "user", "src_ip", "port", "method", "program", "command", "run_as", "target_user"]),
    "v_audit": ("auditd 审计", "logtype = 'audit'", ["action", "result", "user", "src_ip", "exe", "op", "terminal", "uid", "auid"]),
    "v_login": ("登录记录（wtmp 登录/注销/重启、btmp 失败登录）", "logtype IN ('login_wtmp', 'login_btmp')",
                ["action", "result", "user", "tty", "src_ip", "ut_type", "pid"]),
    "v_lastlog": ("账号最近登录快照", "logtype = 'lastlog'", ["user", "uid", "last_login", "tty", "src_ip"]),
    "v_cron": ("定时任务", "logtype = 'cron'", ["action", "user", "command", "target_user", "program"]),
    "v_web_access": ("Web 访问（nginx/httpd）", "logtype IN ('nginx_access', 'httpd_access')",
                     ["client_ip", "method", "url", "status", "bytes", "referer", "user_agent"]),
    "v_web_error": ("Web 错误（nginx/httpd）", "logtype IN ('nginx_error', 'httpd_error')", ["level", "message", "client_ip", "request", "module"]),
}


def hostlogs_views_ddl(db):
    out = []
    for v, (_, where, cols) in HOSTLOG_VIEWS.items():
        sel = ", ".join(f"fields['{c}'] AS \"{c}\"" for c in cols)
        out.append(f"CREATE OR REPLACE VIEW {db}.{v} AS SELECT dt, instance_id, host_name, logtype, path, event_time, "
                   f"{sel}, line, fields FROM {db}.hostlogs WHERE {where}")
    return out


def ensure_views(db):
    for q in hostlogs_views_ddl(db):
        run_sync(q, db=db)


# ALB 访问日志正则（兼容 2016~2025 各代格式）：trace_id 之前为各代共有字段，之后按 AWS 追加顺序逐段可选，
# 缺失字段为 NULL；未来新增字段落入 extra。捕获组固定 35 个，与表列一一对应。
# 注意：不要用 \s —— 写进 DDL 的 SERDEPROPERTIES 后会被转义成字母 s（官方示例正则因此在新格式上会把
# target_status_code_list 与后续字段粘连）。
ALB_REGEX = (r'([^ ]*) ([^ ]*) ([^ ]*) ([^ ]*):([0-9]*) ([^ ]*)[:-]([0-9]*) ([-.0-9]*) ([-.0-9]*) ([-.0-9]*) (|[-0-9]*) '
             r'(-|[-0-9]*) ([-0-9]*) ([-0-9]*) \"([^ ]*) (.*) (- |[^ ]*)\" \"([^\"]*)\" ([A-Z0-9-_]+) ([A-Za-z0-9.-]*) '
             r'([^ ]*) \"([^\"]*)\"'
             r'(?: \"([^\"]*)\" \"([^\"]*)\")?'          # 2017+: domain_name, chosen_cert_arn
             r'(?: ([-.0-9]*) ([^ ]*) \"([^\"]*)\")?'    # 2018+: matched_rule_priority, request_creation_time, actions_executed
             r'(?: \"([^\"]*)\" \"([^ ]*)\")?'           # 2018+: redirect_url, error_reason
             r'(?: \"([^ \"]+)\" \"([^ \"]+)\")?'        # 2019+: target:port_list, target_status_code_list
             r'(?: \"([^ ]*)\" \"([^ ]*)\")?'            # 2020+: classification, classification_reason
             r'(?: ([^ \"]+))?'                          # 2024+: conn_trace_id
             r'( .*)?')                                  # 之后新增字段 → extra


def alb_ddl(db, table, bucket, prefix, account, region, start_day="2015/01/01"):
    pfx = prefix.strip("/")
    loc = f"s3://{bucket}/{pfx + '/' if pfx else ''}AWSLogs/{account}/elasticloadbalancing/{region}/"
    return f"""CREATE EXTERNAL TABLE IF NOT EXISTS `{db}`.`{table}` (
  type string, time string, elb string, client_ip string, client_port int, target_ip string, target_port int,
  request_processing_time double, target_processing_time double, response_processing_time double,
  elb_status_code int, target_status_code string, received_bytes bigint, sent_bytes bigint,
  request_verb string, request_url string, request_proto string, user_agent string, ssl_cipher string, ssl_protocol string,
  target_group_arn string, trace_id string, domain_name string, chosen_cert_arn string, matched_rule_priority string,
  request_creation_time string, actions_executed string, redirect_url string, lambda_error_reason string,
  target_port_list string, target_status_code_list string, classification string, classification_reason string,
  conn_trace_id string, extra string)
PARTITIONED BY (day string)
ROW FORMAT SERDE 'org.apache.hadoop.hive.serde2.RegexSerDe'
WITH SERDEPROPERTIES ('serialization.format'='1','input.regex'='{ALB_REGEX}')
LOCATION '{loc}'
TBLPROPERTIES ('projection.enabled'='true','projection.day.type'='date','projection.day.format'='yyyy/MM/dd',
 'projection.day.range'='{start_day},NOW','projection.day.interval'='1','projection.day.interval.unit'='DAYS',
 'storage.location.template'='{loc}${{day}}')"""


def cloudtrail_ddl(db, bucket, prefix, account, regions):
    pfx = prefix.strip("/")
    loc = f"s3://{bucket}/{pfx + '/' if pfx else ''}AWSLogs/{account}/CloudTrail/"
    return f"""CREATE EXTERNAL TABLE IF NOT EXISTS `{db}`.`cloudtrail` (
  eventversion string,
  useridentity struct<type:string,principalid:string,arn:string,accountid:string,invokedby:string,accesskeyid:string,username:string,
    sessioncontext:struct<attributes:struct<mfaauthenticated:string,creationdate:string>,
    sessionissuer:struct<type:string,principalid:string,arn:string,accountid:string,username:string>>>,
  eventtime string, eventsource string, eventname string, awsregion string, sourceipaddress string, useragent string,
  errorcode string, errormessage string, requestparameters string, responseelements string, additionaleventdata string,
  requestid string, eventid string, readonly string, resources array<struct<arn:string,accountid:string,type:string>>,
  eventtype string, apiversion string, recipientaccountid string, serviceeventdetails string, sharedeventid string,
  vpcendpointid string, eventcategory string, tlsdetails struct<tlsversion:string,ciphersuite:string,clientprovidedhostheader:string>)
PARTITIONED BY (region string, day string)
ROW FORMAT SERDE 'org.apache.hive.hcatalog.data.JsonSerDe'
STORED AS INPUTFORMAT 'com.amazon.emr.cloudtrail.CloudTrailInputFormat'
OUTPUTFORMAT 'org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat'
LOCATION '{loc}'
TBLPROPERTIES ('projection.enabled'='true','projection.region.type'='enum','projection.region.values'='{",".join(regions)}',
 'projection.day.type'='date','projection.day.format'='yyyy/MM/dd','projection.day.range'='2026/01/01,NOW',
 'projection.day.interval'='1','projection.day.interval.unit'='DAYS',
 'storage.location.template'='{loc}${{region}}/${{day}}')"""


RE_TABLE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


def ensure_base_tables(db=GLUE_DB, hostlogs_prefix="hostlogs/"):
    run_sync(f"CREATE DATABASE IF NOT EXISTS `{db}`", db="default")
    run_sync(hostlogs_ddl(db, f"s3://{LOG_BUCKET}/{hostlogs_prefix}"), db=db)
    ensure_views(db)


def drop_table(db, table):
    if not RE_TABLE.match(table):
        raise ApiError(400, "表名非法")
    run_sync(f"DROP TABLE IF EXISTS `{db}`.`{table}`", db=db)
