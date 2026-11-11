"""主机日志：历史查询构造器 + 预置审计场景（Athena SQL 与实时 grep 正则双形态）。"""
from common import ApiError, sql_str, v_date, v_iid

SCENARIOS = [
    {"id": "ssh_bruteforce", "name": "SSH 暴力破解", "level": "high", "logtypes": ["auth"],
     "desc": "按来源 IP 统计 SSH 认证失败（Failed password / Invalid user），超过阈值视为暴力破解。",
     "grep": r"Failed password|Invalid user|authentication failure", "params": {"threshold": 10},
     "sql": """SELECT host_name, instance_id,
  coalesce(regexp_extract(line, 'from ([0-9a-fA-F.:]+)', 1), regexp_extract(line, 'rhost=([0-9a-fA-F.:]+)', 1)) AS src_ip,
  count(*) AS failures, min(event_time) AS first_seen, max(event_time) AS last_seen,
  array_join(slice(array_sort(array_agg(DISTINCT regexp_extract(line, 'for (invalid user )?(\\S+) from', 2))), 1, 8), ',') AS users_tried
FROM hostlogs WHERE {where} AND regexp_like(line, 'Failed password|Invalid user .* from|authentication failure')
GROUP BY 1, 2, 3 HAVING count(*) >= {threshold} ORDER BY failures DESC LIMIT 200"""},
    {"id": "ssh_success", "name": "SSH 登录成功明细", "level": "info", "logtypes": ["auth"],
     "desc": "所有 Accepted 登录：用户、来源 IP、认证方式。", "grep": r"Accepted (password|publickey|keyboard-interactive)",
     "sql": """SELECT event_time, host_name, regexp_extract(line, 'Accepted (\\S+) for (\\S+) from (\\S+)', 2) AS user,
  regexp_extract(line, 'Accepted (\\S+) for (\\S+) from (\\S+)', 3) AS src_ip,
  regexp_extract(line, 'Accepted (\\S+) for', 1) AS method
FROM hostlogs WHERE {where} AND line LIKE '%Accepted % for % from %' ORDER BY event_time DESC LIMIT 500"""},
    {"id": "root_login", "name": "root 直接登录", "level": "high", "logtypes": ["auth"],
     "desc": "root 通过 SSH 直接登录成功（应禁用 PermitRootLogin）。", "grep": r"Accepted \S+ for root from",
     "sql": """SELECT event_time, host_name, instance_id, regexp_extract(line, 'from (\\S+)', 1) AS src_ip,
  regexp_extract(line, 'Accepted (\\S+) for', 1) AS method, line
FROM hostlogs WHERE {where} AND regexp_like(line, 'Accepted \\S+ for root from') ORDER BY event_time DESC LIMIT 500"""},
    {"id": "success_after_fail", "name": "失败后成功登录（疑似破解成功）", "level": "critical", "logtypes": ["auth"],
     "desc": "同一主机当天先出现 ≥{threshold} 次失败，随后出现密码登录成功。", "params": {"threshold": 10}, "grep": None,
     "sql": """WITH f AS (SELECT instance_id, host_name, count(*) AS failures, min(event_time) AS first_fail FROM hostlogs
  WHERE {where} AND line LIKE '%Failed password%' GROUP BY 1, 2 HAVING count(*) >= {threshold}),
s AS (SELECT instance_id, event_time, regexp_extract(line, 'for (\\S+) from (\\S+)', 1) AS user,
  regexp_extract(line, 'for (\\S+) from (\\S+)', 2) AS src_ip FROM hostlogs WHERE {where} AND line LIKE '%Accepted password for%')
SELECT f.host_name, f.instance_id, f.failures, f.first_fail, s.event_time AS success_time, s.user, s.src_ip
FROM f JOIN s ON f.instance_id = s.instance_id AND s.event_time >= f.first_fail ORDER BY s.event_time LIMIT 200"""},
    {"id": "sudo_exec", "name": "sudo 提权命令", "level": "medium", "logtypes": ["auth"],
     "desc": "sudo 执行的命令，高亮敏感命令（shadow/passwd/visudo/chmod 777 等）。", "grep": r"sudo:.*COMMAND=",
     "sql": """SELECT event_time, host_name, trim(regexp_extract(line, 'sudo:\\s+(\\S+)\\s*:', 1)) AS user,
  regexp_extract(line, 'USER=(\\S+)', 1) AS as_user, regexp_extract(line, 'COMMAND=(.*)$', 1) AS command,
  regexp_like(line, '/etc/shadow|/etc/sudoers|visudo|passwd |chmod 777|useradd|usermod|/bin/bash|/bin/sh') AS sensitive
FROM hostlogs WHERE {where} AND line LIKE '%sudo:%COMMAND=%' ORDER BY event_time DESC LIMIT 500"""},
    {"id": "account_change", "name": "账号与口令变更", "level": "high", "logtypes": ["auth", "audit"],
     "desc": "新增/删除/修改用户与组、口令变更。", "grep": r"useradd|userdel|usermod|groupadd|password changed|ADD_USER|DEL_USER",
     "sql": """SELECT event_time, host_name, logtype,
  regexp_extract(line, '(useradd|userdel|usermod|groupadd|groupdel|passwd|chpasswd|ADD_USER|DEL_USER|USER_MGMT)', 1) AS action,
  coalesce(regexp_extract(line, 'name=([^,\\s]+)', 1), regexp_extract(line, 'password changed for (\\S+)', 1), regexp_extract(line, 'id=(\\d+)', 1)) AS target,
  line
FROM hostlogs WHERE {where} AND regexp_like(line, 'useradd\\[|userdel\\[|usermod\\[|groupadd\\[|groupdel\\[|password changed for|type=ADD_USER|type=DEL_USER|type=USER_MGMT')
ORDER BY event_time DESC LIMIT 500"""},
    {"id": "cron_change", "name": "定时任务变更", "level": "medium", "logtypes": ["cron"],
     "desc": "crontab 被替换/编辑/删除。", "grep": r"crontab.*(REPLACE|BEGIN EDIT|END EDIT|DELETE)",
     "sql": """SELECT event_time, host_name, regexp_extract(line, '\\((\\S+)\\) (REPLACE|BEGIN EDIT|END EDIT|DELETE|LIST)', 1) AS user,
  regexp_extract(line, '(REPLACE|BEGIN EDIT|END EDIT|DELETE)', 1) AS action, line
FROM hostlogs WHERE {where} AND regexp_like(line, 'crontab\\[\\d+\\].*(REPLACE|BEGIN EDIT|END EDIT|DELETE)') ORDER BY event_time DESC LIMIT 500"""},
    {"id": "audit_login_failed", "name": "auditd 登录失败", "level": "medium", "logtypes": ["audit"],
     "desc": "audit.log 中 USER_LOGIN/USER_AUTH res=failed。", "grep": r"type=USER_(LOGIN|AUTH).*res=failed",
     "sql": """SELECT host_name, regexp_extract(line, 'acct="?([^" ]+)', 1) AS acct, regexp_extract(line, 'addr=(\\S+)', 1) AS addr,
  count(*) AS n, min(event_time) AS first_seen, max(event_time) AS last_seen
FROM hostlogs WHERE {where} AND logtype = 'audit' AND regexp_like(line, 'type=USER_(LOGIN|AUTH) .*res=failed')
GROUP BY 1, 2, 3 ORDER BY n DESC LIMIT 200"""},
    {"id": "btmp_failed", "name": "btmp 失败登录记录", "level": "medium", "logtypes": ["login_btmp"],
     "desc": "lastb 转换出的失败登录（用户/来源）。", "grep": None,
     "sql": """SELECT host_name, regexp_extract(line, '^(\\S+)', 1) AS user, regexp_extract(line, '^\\S+\\s+\\S+\\s+(\\S+)', 1) AS src,
  count(*) AS n, max(event_time) AS last_seen
FROM hostlogs WHERE {where} AND logtype = 'login_btmp' GROUP BY 1, 2, 3 ORDER BY n DESC LIMIT 200"""},
    {"id": "web_errors", "name": "Web 4xx/5xx 与可疑路径", "level": "low", "logtypes": ["nginx_access", "httpd_access"],
     "desc": "Nginx/httpd 访问日志按状态码与 URL 聚合，标出扫描类路径。", "grep": r"\" (4|5)[0-9]{2} ",
     "sql": """SELECT host_name, regexp_extract(line, '\\" (\\d{3}) ', 1) AS status,
  regexp_extract(line, '\\"[A-Z]+ (\\S+)', 1) AS url, count(*) AS n,
  regexp_like(regexp_extract(line, '\\"[A-Z]+ (\\S+)', 1), '\\.env|wp-login|phpmyadmin|\\.git/|actuator|cgi-bin') AS suspicious
FROM hostlogs WHERE {where} AND logtype IN ('nginx_access', 'httpd_access') AND regexp_like(line, '\\" [45]\\d{2} ')
GROUP BY 1, 2, 3 ORDER BY n DESC LIMIT 200"""},
]
SCN = {s["id"]: s for s in SCENARIOS}


def where_clause(f):
    d1, d2 = v_date(f.get("date_from")), v_date(f.get("date_to") or f.get("date_from"))
    w = [f"dt BETWEEN {sql_str(d1)} AND {sql_str(d2)}"]
    ids = [v_iid(i) for i in (f.get("instance_ids") or [])][:200]
    if ids:
        w.append("instance_id IN (" + ",".join(sql_str(i) for i in ids) + ")")
    if f.get("logtypes"):
        w.append("logtype IN (" + ",".join(sql_str(x[:32]) for x in f["logtypes"][:20]) + ")")
    if f.get("path"):
        w.append(f"path = {sql_str(f['path'][:256])}")
    if f.get("time_from"):
        w.append(f"event_time >= {sql_str(f['time_from'][:19])}")
    if f.get("time_to"):
        w.append(f"event_time <= {sql_str(f['time_to'][:19])}")
    return " AND ".join(w)


def history_sql(f):
    w = where_clause(f)
    kw = (f.get("keyword") or "").strip()
    if kw:
        if f.get("regex"):
            w += f" AND regexp_like(line, {sql_str(kw[:300])})"
        else:
            w += f" AND strpos(lower(line), lower({sql_str(kw[:300])})) > 0"
    lim = max(1, min(int(f.get("limit") or 500), 2000))
    return (f"SELECT event_time, host_name, instance_id, path, logtype, line, fields FROM hostlogs WHERE {w} "
            f"ORDER BY event_time DESC NULLS LAST, line_no DESC LIMIT {lim}")


def scenario_sql(sid, f):
    s = SCN.get(sid)
    if not s:
        raise ApiError(404, "场景不存在")
    params = dict(s.get("params") or {})
    for k in params:
        if k in f:
            params[k] = max(1, min(int(f[k]), 100000))
    f2 = dict(f)
    if not f2.get("logtypes"):
        f2.pop("logtypes", None)
    sql = s["sql"].replace("{where}", where_clause(f2))
    for k, v in params.items():
        sql = sql.replace("{" + k + "}", str(int(v)))
    return sql
