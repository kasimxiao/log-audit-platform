"""ALB 访问日志：数据源（桶/前缀可配，一源一表）+ 预置报表 + 条件查询 + 地理分布。"""
import ipaddress, os, re
from datetime import date, timedelta
from functools import lru_cache

from common import ACCOUNT_ID, ApiError, GLUE_DB, HOME_REGION, client, delete, get, now_iso, put, query, sql_str, v_region
import athena_svc

RE_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
RE_PREFIX = re.compile(r"^[A-Za-z0-9!_.*'()/-]{0,200}$")
RE_ID = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
RE_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def list_sources():
    return [{k: v for k, v in i.items() if k not in ("pk", "sk")} for i in query("ALBSRC")]


def get_source(sid):
    s = get("ALBSRC", sid)
    if not s:
        raise ApiError(404, f"数据源不存在: {sid}")
    return s


def detect_with_access(bucket, prefix):
    """管理员探测未登记的桶：先临时把该桶加入读权限（未登记的话下次同步时会移除）。"""
    import source_access
    source_access.sync(extra=[{"bucket": bucket, "prefix": prefix}], wait_bucket={"bucket": bucket, "prefix": prefix})
    return detect(bucket, prefix)


def detect(bucket, prefix):
    """探测桶内 ALB 日志：返回区域 / 账号 / 负载均衡器名样例。"""
    s3 = client("s3")
    pfx = (prefix.strip("/") + "/" if prefix.strip("/") else "") + "AWSLogs/"
    accts = [p["Prefix"].split("/")[-2] for p in s3.list_objects_v2(Bucket=bucket, Prefix=pfx, Delimiter="/").get("CommonPrefixes", [])]
    out = []
    for a in accts[:5]:
        rp = f"{pfx}{a}/elasticloadbalancing/"
        regs = [p["Prefix"].split("/")[-2] for p in s3.list_objects_v2(Bucket=bucket, Prefix=rp, Delimiter="/").get("CommonPrefixes", [])]
        out += [{"account": a, "region": r} for r in regs]
    return out


def create_source(body, actor):
    sid = (body.get("id") or "").strip().lower()
    if not RE_ID.match(sid):
        raise ApiError(400, "ID 需小写字母开头，仅含小写字母数字下划线（2-31 位）")
    if get("ALBSRC", sid):
        raise ApiError(409, "数据源 ID 已存在")
    bucket, prefix = body.get("bucket", "").strip(), body.get("prefix", "").strip()
    if not RE_BUCKET.match(bucket) or not RE_PREFIX.match(prefix):
        raise ApiError(400, "桶名或前缀非法")
    import source_access
    source_access.sync(extra=[{"bucket": bucket, "prefix": prefix}], wait_bucket={"bucket": bucket, "prefix": prefix})
    try:
        found = detect(bucket, prefix)
    except Exception as e:  # noqa
        raise ApiError(400, f"无法读取桶 {bucket}: {str(e)[:200]}")
    region = body.get("region") or (found[0]["region"] if found else HOME_REGION)
    account = body.get("account") or (found[0]["account"] if found else ACCOUNT_ID)
    v_region(region)
    if not re.match(r"^\d{12}$", account):
        raise ApiError(400, "账号 ID 非法")
    table = f"alb_{sid}"
    athena_svc.run_sync(athena_svc.alb_ddl(GLUE_DB, table, bucket, prefix, account, region))
    item = {"id": sid, "name": (body.get("name") or sid)[:60], "bucket": bucket, "prefix": prefix, "region": region,
            "account": account, "table": table, "created_by": actor, "created_at": now_iso(), "detected": found}
    put("ALBSRC", sid, **item)
    source_access.sync()
    return item


def delete_source(sid):
    s = get_source(sid)
    athena_svc.drop_table(GLUE_DB, s["table"])
    delete("ALBSRC", sid)
    import source_access
    source_access.sync()
    return {"ok": True}


def list_elbs():
    """当前账号各区域的 ALB 及访问日志开关（帮助配置数据源）。"""
    from hosts import enabled_regions
    out = []
    for r in [HOME_REGION] + [x for x in enabled_regions() if x != HOME_REGION]:
        try:
            lbs = client("elbv2", r).describe_load_balancers()["LoadBalancers"]
        except Exception:  # noqa
            continue
        for lb in lbs:
            if lb["Type"] != "application":
                continue
            attrs = {a["Key"]: a["Value"] for a in client("elbv2", r).describe_load_balancer_attributes(
                LoadBalancerArn=lb["LoadBalancerArn"])["Attributes"]}
            out.append({"region": r, "name": lb["LoadBalancerName"], "scheme": lb["Scheme"],
                        "elb_id": "app/" + lb["LoadBalancerArn"].split(":loadbalancer/app/")[1],
                        "logs_enabled": attrs.get("access_logs.s3.enabled") == "true",
                        "bucket": attrs.get("access_logs.s3.bucket"), "prefix": attrs.get("access_logs.s3.prefix")})
        if len(out) > 200:
            break
    return out


# ------------------------------------------------------------------ 报表
def _days(f):
    d1, d2 = f.get("date_from"), f.get("date_to") or f.get("date_from")
    if not (d1 and RE_DAY.match(d1) and RE_DAY.match(d2)):
        raise ApiError(400, "日期格式 YYYY-MM-DD")
    if date.fromisoformat(d2) < date.fromisoformat(d1) or (date.fromisoformat(d2) - date.fromisoformat(d1)).days > 92:
        raise ApiError(400, "日期范围 0~92 天")
    return d1.replace("-", "/"), d2.replace("-", "/")


def where(f):
    a, b = _days(f)
    w = [f"day BETWEEN {sql_str(a)} AND {sql_str(b)}"]
    if f.get("elb"):
        w.append(f"elb = {sql_str(f['elb'][:200])}")
    if f.get("client_ip"):
        w.append(f"client_ip = {sql_str(f['client_ip'][:45])}")
    if f.get("status"):
        st = str(f["status"])
        if re.match(r"^[1-5]xx$", st):
            w.append(f"elb_status_code BETWEEN {st[0]}00 AND {st[0]}99")
        elif st.isdigit():
            w.append(f"elb_status_code = {int(st)}")
    if f.get("url_contains"):
        w.append(f"strpos(request_url, {sql_str(f['url_contains'][:300])}) > 0")
    if f.get("method"):
        w.append(f"request_verb = {sql_str(f['method'][:10].upper())}")
    if f.get("min_latency"):
        w.append(f"target_processing_time >= {float(f['min_latency'])}")
    if f.get("time_from"):
        w.append(f"time >= {sql_str(f['time_from'][:30])}")
    if f.get("time_to"):
        w.append(f"time <= {sql_str(f['time_to'][:30])}")
    return " AND ".join(w)


BUCKET_EXPR = {"hour": "date_format(date_trunc('hour', from_iso8601_timestamp(time)), '%Y-%m-%d %H:00')",
               "day": "replace(day, '/', '-')", "minute": "date_format(date_trunc('minute', from_iso8601_timestamp(time)), '%Y-%m-%d %H:%i')"}

REPORTS = {
    "overview": ("总览", """SELECT count(*) AS requests, count(DISTINCT client_ip) AS unique_ips,
  round(100.0 * count_if(elb_status_code BETWEEN 400 AND 499) / greatest(count(*), 1), 2) AS pct_4xx,
  round(100.0 * count_if(elb_status_code >= 500) / greatest(count(*), 1), 2) AS pct_5xx,
  round(avg(nullif(target_processing_time, -1)) * 1000, 1) AS avg_ms,
  round(approx_percentile(nullif(target_processing_time, -1), 0.95) * 1000, 1) AS p95_ms,
  round(sum(sent_bytes) / 1048576.0, 2) AS sent_mb FROM {t} WHERE {w}"""),
    "trend": ("请求与状态码趋势", """SELECT {b} AS bucket, count(*) AS total, count_if(elb_status_code < 400) AS ok,
  count_if(elb_status_code BETWEEN 400 AND 499) AS c4xx, count_if(elb_status_code >= 500) AS c5xx,
  round(approx_percentile(nullif(target_processing_time, -1), 0.95) * 1000, 1) AS p95_ms
FROM {t} WHERE {w} GROUP BY 1 ORDER BY 1 LIMIT 2000"""),
    "status": ("状态码分布", "SELECT elb_status_code, target_status_code, count(*) AS n FROM {t} WHERE {w} GROUP BY 1, 2 ORDER BY n DESC LIMIT 50"),
    "top_ip": ("Top 客户端 IP", """SELECT client_ip, count(*) AS requests, count_if(elb_status_code >= 400) AS errors,
  count(DISTINCT url_extract_path(request_url)) AS distinct_paths, min(time) AS first_seen, max(time) AS last_seen
FROM {t} WHERE {w} GROUP BY 1 ORDER BY requests DESC LIMIT 50"""),
    "top_url": ("Top URL", """SELECT url_extract_path(request_url) AS path, request_verb, count(*) AS requests,
  count_if(elb_status_code >= 500) AS c5xx, round(avg(nullif(target_processing_time, -1)) * 1000, 1) AS avg_ms
FROM {t} WHERE {w} GROUP BY 1, 2 ORDER BY requests DESC LIMIT 50"""),
    "errors_5xx": ("5xx 明细（按 ELB/目标/时段）", """SELECT {b} AS bucket, elb, elb_status_code, target_status_code,
  coalesce(nullif(target_ip, '-'), '(无目标)') AS target, count(*) AS n FROM {t} WHERE {w} AND elb_status_code >= 500
GROUP BY 1, 2, 3, 4, 5 ORDER BY n DESC LIMIT 200"""),
    "slow": ("慢请求", """SELECT url_extract_path(request_url) AS path, count(*) AS n,
  round(avg(target_processing_time), 3) AS avg_s, round(max(target_processing_time), 3) AS max_s
FROM {t} WHERE {w} AND target_processing_time >= {slow} GROUP BY 1 ORDER BY n DESC LIMIT 50"""),
    "user_agent": ("User-Agent 分布", "SELECT user_agent, count(*) AS n FROM {t} WHERE {w} GROUP BY 1 ORDER BY n DESC LIMIT 30"),
    "scan": ("疑似扫描/攻击", """SELECT client_ip, count(*) AS requests, count_if(elb_status_code = 404) AS c404,
  round(100.0 * count_if(elb_status_code = 404) / count(*), 1) AS pct_404,
  count_if(regexp_like(lower(request_url), '\\.env|wp-login|phpmyadmin|\\.git/|actuator|cgi-bin|/etc/passwd|union.*select|<script')) AS sensitive_hits,
  array_join(slice(array_agg(DISTINCT url_extract_path(request_url)), 1, 6), ' ') AS sample_paths
FROM {t} WHERE {w} GROUP BY 1
HAVING count_if(regexp_like(lower(request_url), '\\.env|wp-login|phpmyadmin|\\.git/|actuator|cgi-bin|/etc/passwd|union.*select|<script')) > 0
   OR (count(*) >= 50 AND count_if(elb_status_code = 404) * 1.0 / count(*) > 0.5)
ORDER BY sensitive_hits DESC, requests DESC LIMIT 50"""),
    "elb": ("各负载均衡器", """SELECT elb, count(*) AS requests, count_if(elb_status_code >= 500) AS c5xx,
  round(approx_percentile(nullif(target_processing_time, -1), 0.95) * 1000, 1) AS p95_ms FROM {t} WHERE {w} GROUP BY 1 ORDER BY 2 DESC"""),
    "format": ("日志格式与解析率", """SELECT elb,
  CASE WHEN elb IS NULL THEN '解析失败' WHEN domain_name IS NULL THEN '2016 版（至 trace_id）'
       WHEN actions_executed IS NULL THEN '2017 版（+域名/证书）' WHEN target_port_list IS NULL THEN '2018 版（+规则/动作/重定向）'
       WHEN classification IS NULL THEN '2019 版（+目标列表）' WHEN conn_trace_id IS NULL THEN '2020 版（+分类）'
       WHEN extra IS NULL THEN '2024 版（+连接追踪）' ELSE '2025 版及以后（+转换等新增字段）' END AS format,
  count(*) AS n, min(time) AS first_seen, max(time) AS last_seen
FROM {t} WHERE {w} GROUP BY 1, 2 ORDER BY 1, 2"""),
    "geo": ("地理分布（Top 500 IP）", "SELECT client_ip, count(*) AS n FROM {t} WHERE {w} GROUP BY 1 ORDER BY n DESC LIMIT 500"),
}


def report_sql(sid, rid, f):
    s = get_source(sid)
    if rid not in REPORTS:
        raise ApiError(404, "报表不存在")
    b = BUCKET_EXPR.get(f.get("bucket") or "hour", BUCKET_EXPR["hour"])
    slow = max(0.1, min(float(f.get("slow_threshold") or 1.0), 600))
    return REPORTS[rid][1].replace("{t}", s["table"]).replace("{w}", where(f)).replace("{b}", b).replace("{slow}", str(slow))


def search_sql(sid, f):
    s = get_source(sid)
    lim = max(1, min(int(f.get("limit") or 200), 2000))
    return (f"SELECT time, elb, client_ip, request_verb, request_url, elb_status_code, target_status_code, "
            f"target_processing_time, sent_bytes, user_agent, target_ip FROM {s['table']} WHERE {where(f)} "
            f"ORDER BY time DESC LIMIT {lim}")


# ------------------------------------------------------------------ GeoIP（DB-IP Lite, CC BY 4.0）
@lru_cache(maxsize=1)
def _geo_reader():
    import maxminddb
    p = os.path.join(os.path.dirname(__file__), "geo", "dbip-country-lite.mmdb")
    return maxminddb.open_database(p) if os.path.exists(p) else None


def geo_country(ip):
    try:
        a = ipaddress.ip_address(ip)
        if a.is_private or a.is_loopback or a.is_link_local:
            return {"code": "LAN", "name": "内网"}
        r = _geo_reader()
        rec = r.get(ip) if r else None
        if rec and rec.get("country"):
            c = rec["country"]
            return {"code": c.get("iso_code"), "name": (c.get("names") or {}).get("zh-CN") or (c.get("names") or {}).get("en")}
    except Exception:  # noqa
        pass
    return {"code": "??", "name": "未知"}


def geo_enrich(columns, rows):
    """geo 报表：按国家聚合；其他含 client_ip 的结果：追加国家列。"""
    if "client_ip" not in columns:
        return columns, rows, None
    ix = columns.index("client_ip")
    rows2 = [r + [geo_country(r[ix])["name"]] for r in rows]
    agg = {}
    if "n" in columns:
        nx = columns.index("n")
        for r in rows:
            g = geo_country(r[ix])
            k = (g["code"], g["name"])
            agg[k] = agg.get(k, 0) + int(r[nx] or 0)
    countries = [{"code": k[0], "name": k[1], "requests": v} for k, v in sorted(agg.items(), key=lambda x: -x[1])]
    return columns + ["country"], rows2, countries
