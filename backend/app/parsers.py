"""入库时的字段预解析：按 logtype 从原始行提取常用字段到 fields（map<string,string>）。
统一键名：src_ip / user / action / result / command / status / url / method / client_ip / program / pid / message。
解析不到的字段不出现；原文 line 始终保留，查询时仍可正则兜底。"""
import json, re

SYSLOG = re.compile(r"^(?:[A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2}|\d{4}-\d{2}-\d{2}T\S+)\s+(\S+)\s+([\w./@()-]+?)(?:\[(\d+)\])?:\s?(.*)$")

AUTH_RULES = [
    (re.compile(r"Failed (\S+) for (invalid user )?(\S+) from (\S+) port (\d+)"),
     lambda m: {"action": "failed_password" if m[1] == "password" else f"failed_{m[1]}", "method": m[1], "user": m[3],
                "src_ip": m[4], "port": m[5], "result": "failed", **({"invalid_user": "true"} if m[2] else {})}),
    (re.compile(r"Accepted (\S+) for (\S+) from (\S+) port (\d+)"),
     lambda m: {"action": "accepted", "method": m[1], "user": m[2], "src_ip": m[3], "port": m[4], "result": "success"}),
    (re.compile(r"Invalid user (\S*) from (\S+)(?: port (\d+))?"),
     lambda m: {"action": "invalid_user", "user": m[1], "src_ip": m[2], "result": "failed", **({"port": m[3]} if m[3] else {})}),
    (re.compile(r"authentication failure;.*?rhost=(\S*)(?:\s+user=(\S+))?"),
     lambda m: {"action": "auth_failure", "result": "failed", **({"src_ip": m[1]} if m[1] else {}), **({"user": m[2]} if m[2] else {})}),
    (re.compile(r"Connection closed by (?:(authenticating|invalid) user (\S+) )?(\S+) port (\d+)"),
     lambda m: {"action": "connection_closed", "src_ip": m[3], "port": m[4], **({"user": m[2]} if m[2] else {})}),
    (re.compile(r"sudo(?:\[\d+\])?:\s+(\S+)\s*:\s*(?:((?!TTY=|PWD=)[^;]*?) ; )?(?:TTY=(\S+) ; )?PWD=(\S+) ; USER=(\S+) ; COMMAND=(.*)$"),
     lambda m: {"action": "sudo", "user": m[1], "tty": m[3] or "", "pwd": m[4], "run_as": m[5], "command": m[6],
                "result": "failed" if m[2] else "success", **({"reason": m[2]} if m[2] else {})}),
    (re.compile(r"session opened for user (\S+?)(?:\(uid=\d+\))?(?: by (\S+?)(?:\(uid=\d+\))?)?$"),
     lambda m: {"action": "session_opened", "user": m[1], **({"by": m[2]} if m[2] else {})}),
    (re.compile(r"session closed for user (\S+)"), lambda m: {"action": "session_closed", "user": m[1]}),
    (re.compile(r"useradd\[\d+\]: new user: name=([^,]+), UID=(\d+)"), lambda m: {"action": "useradd", "target_user": m[1], "uid": m[2]}),
    (re.compile(r"useradd\[\d+\]: new group: name=([^,]+)"), lambda m: {"action": "groupadd", "target_group": m[1]}),
    (re.compile(r"userdel\[\d+\]: delete user '([^']+)'"), lambda m: {"action": "userdel", "target_user": m[1]}),
    (re.compile(r"usermod\[\d+\]: (.*)$"), lambda m: {"action": "usermod", "detail": m[1]}),
    (re.compile(r"groupadd\[\d+\]: .*name=([^,\s]+)"), lambda m: {"action": "groupadd", "target_group": m[1]}),
    (re.compile(r"password changed for (\S+)"), lambda m: {"action": "password_changed", "target_user": m[1]}),
    (re.compile(r"su(?:\[\d+\])?: (?:\(to (\S+)\) (\S+)|pam_unix\(su(?:-l)?:session\): session opened for user (\S+?)(?:\(uid=\d+\))? by (\S+?)(?:\(uid=\d+\))?)"),
     lambda m: {"action": "su", "target_user": m[1] or m[3], "user": m[2] or m[4] or ""}),
]
CRON_RULES = [
    (re.compile(r"\((\S+)\) CMD(END)? \((.*)\)\s*$"), lambda m: {"action": "cmd_end" if m[2] else "cmd_run", "user": m[1], "command": m[3]}),
    (re.compile(r"crontab\[\d+\]: \((\S+)\) (REPLACE|BEGIN EDIT|END EDIT|DELETE|LIST) \((\S+)\)"),
     lambda m: {"action": m[2].lower().replace(" ", "_"), "user": m[1], "target_user": m[3]}),
]
KV = re.compile(r"(\w+)=(\"[^\"]*\"|'[^']*'|\S+)")
ACCESS = re.compile(r'^(\S+) \S+ (\S+) \[([^\]]+)\] "(\S+) (\S+)(?: ([^"]*))?" (\d{3}) (\S+)(?: "([^"]*)" "([^"]*)")?')
NGX_ERR = re.compile(r"^\S+ \S+ \[(\w+)\] (\d+)#\d+: (?:\*(\d+) )?(.*)$")
NGX_CTX = re.compile(r', (client|server|request|upstream|host|referrer): ("[^"]*"|[^,]+)')
HTTPD_ERR = re.compile(r"^\[[^\]]+\] \[(?:(\w+):)?(\w+)\] (?:\[pid (\d+)(?::tid \d+)?\] )?(?:\[client ([^\]]+?)(?::\d+)?\] )?(.*)$")
LAST_TXT = re.compile(r"^(\S+)\s+(\S+)\s+(\S+)\s+")


def _apply(rules, text):
    for rx, fn in rules:
        m = rx.search(text)
        if m:
            return {k: v for k, v in fn(m).items() if v is not None}
    return {}


def _syslog(line):
    m = SYSLOG.match(line)
    if not m:
        return {}, line
    return {"host": m[1], "program": m[2], **({"pid": m[3]} if m[3] else {})}, m[4]


def _audit(line):
    f = {}
    for k, v in KV.findall(line):
        v = v.strip("\"'")
        if k == "msg":  # msg=audit(...) 是时间戳；msg='k=v ...' 在下面展开
            continue
        if k not in f:
            f[k] = v
        if len(f) >= 40:
            break
    m = re.search(r"msg='([^']*)'", line)  # 内层 msg='k=v ...'
    if m:
        for k, v in KV.findall(m[1]):
            f.setdefault(k, v.strip("\"'"))
    if f.get("addr") and f["addr"] not in ("?", "0.0.0.0"):
        f["src_ip"] = f["addr"]
    if f.get("acct"):
        f["user"] = f["acct"]
    if f.get("res"):
        f["result"] = "success" if f["res"] in ("success", "yes", "1") else "failed"
    t = re.match(r"type=(\S+)", line)
    if t:
        f["action"] = t[1].lower()
    return f


def parse_fields(logtype, line):
    """返回 dict[str, str]；任何异常都返回 {}（不影响入库）。"""
    try:
        s = line.lstrip()
        if s.startswith("{") and s.rstrip().endswith("}"):  # JSON 行（应用日志）
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    return {str(k)[:64]: (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))[:2000]
                            for k, v in list(obj.items())[:40] if v is not None}
            except ValueError:
                pass
        if logtype == "audit":
            return _audit(line)
        if logtype in ("nginx_access", "httpd_access"):
            m = ACCESS.match(line)
            if m:
                return {k: v for k, v in {"client_ip": m[1], "remote_user": m[2], "method": m[4], "url": m[5],
                                          "protocol": m[6], "status": m[7], "bytes": m[8], "referer": m[9],
                                          "user_agent": m[10]}.items() if v not in (None, "-")}
            return {}
        if logtype == "nginx_error":
            m = NGX_ERR.match(line)
            if not m:
                return {}
            rest = m[4]; ctx = {k: v.strip('"') for k, v in NGX_CTX.findall(rest)}
            i = rest.find(", client: ")
            f = {"level": m[1], "pid": m[2], "message": (rest[:i] if i >= 0 else rest)[:1000], **({"connection": m[3]} if m[3] else {}),
                 **{("client_ip" if k == "client" else k): v for k, v in ctx.items()}}
            return f
        if logtype == "httpd_error":
            m = HTTPD_ERR.match(line)
            return {k: v for k, v in {"module": m[1], "level": m[2], "pid": m[3], "client_ip": m[4], "message": m[5][:1000]}.items()
                    if v} if m else {}
        if logtype in ("login_wtmp", "login_btmp"):  # 旧版 last 文本输出
            m = LAST_TXT.match(line)
            if m and m[1] not in ("wtmp", "btmp"):
                if logtype == "login_btmp":
                    return {"action": "failed_login", "result": "failed", "user": m[1], "tty": m[2], "src_ip": m[3]}
                if m[1] == "reboot":
                    return {"action": "boot", "user": "reboot"}
                return {"action": "login", "result": "success", "user": m[1], "tty": m[2], "src_ip": m[3]}
            return {}
        if logtype == "lastlog":  # 旧版 lastlog 命令输出
            m = re.match(r"^(\S+)\s+(.*)$", line)
            if m and m[1] != "Username":
                return {"user": m[1], **({"last_login": "never"} if "Never logged in" in line else {})}
            return {}
        base, msg = _syslog(line)
        if logtype == "auth":
            return {**base, **_apply(AUTH_RULES, line)}
        if logtype == "cron":
            return {**base, **_apply(CRON_RULES, line)}
        if base:  # syslog / custom 等 syslog 格式
            return {**base, "message": msg[:1000]}
    except Exception:  # noqa
        pass
    return {}
