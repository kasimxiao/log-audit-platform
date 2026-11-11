#!/usr/bin/env python3
"""确定性合成数据：ALB 访问日志 + 主机日志（与 extractor 输出格式一致）。

用途：1) 评估库 logaudit_eval 的固定数据；2) 主库"合成演示"ALB 数据源。
输出到 data/synthetic/{alb,hostlogs}，再由 deploy 脚本上传到 S3。
用法：python3 scripts/gen_synthetic.py <account_id> [region]
"""
import gzip, json, os, random, sys
from datetime import datetime, timedelta, timezone

ACCT = sys.argv[1] if len(sys.argv) > 1 else "123456789012"
REGION = sys.argv[2] if len(sys.argv) > 2 else "ap-northeast-1"
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "synthetic")
DAYS = ["2026-11-01", "2026-11-02", "2026-11-03"]
rnd = random.Random(20261111)

# ---------------------------------------------------------------- ALB
ELBS = {
    "shop-web": ("app/shop-web/5a1c0e0d7f3b9a21", "shop.anycompany.com", "tg-shop-web/6d0ecf831eec9f09"),
    "shop-api": ("app/shop-api/8f2b6c4d1e0a3b57", "api.anycompany.com", "tg-shop-api/3a9d2c7e5b1f8046"),
}
CLIENT_POOL = [  # 常规用户 IP（多国）
    "1.36.12.88", "14.215.177.38", "36.112.35.17", "58.247.12.101", "101.6.15.130",
    "110.242.68.66", "113.108.81.189", "120.92.78.97", "203.208.40.66", "61.135.169.121",
    "8.8.4.4", "23.21.150.121", "52.95.110.1", "64.233.160.0", "98.137.11.163",
    "126.209.1.33", "133.242.0.5", "153.120.1.7", "210.140.92.181", "219.94.129.4",
    "81.2.69.142", "85.214.132.117", "91.198.174.192", "151.101.1.69", "195.154.122.1",
    "103.4.96.10", "202.166.196.1", "175.45.176.3", "49.51.8.12", "43.128.2.9",
]
SCANNER_IP = "45.155.205.233"    # 11-02 扫描敏感路径
BURST_DAY, BURST_HOUR = "2026-11-02", 14   # shop-api 14:00-14:15 5xx 爆发
SLOW_PATH = "/api/v1/report/export"        # 慢接口
UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "okhttp/4.12.0", "AnyCompanyApp/5.2.1 (Android 15)", "curl/8.5.0",
]
WEB_PATHS = ["/", "/index.html", "/product/1001", "/product/1002", "/product/2003", "/cart", "/static/app.js", "/static/app.css", "/search?q=phone"]
API_PATHS = ["/api/v1/products", "/api/v1/cart", "/api/v1/orders", "/api/v1/user/profile", "/api/v1/login", SLOW_PATH]
SCAN_PATHS = ["/.env", "/wp-login.php", "/phpmyadmin/", "/.git/config", "/admin/config.php", "/actuator/env", "/cgi-bin/luci"]


def alb_line(ts, elb_key, cip, verb, path, status, tstatus, tpt, ua, sent):
    elb, host, tg = ELBS[elb_key]
    tgt = f"10.100.11.{rnd.randint(10, 60)}:80"
    t = ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    ct = (ts - timedelta(milliseconds=int(tpt * 1000) + 3)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    tpt_s = "-1" if tstatus == "-" else f"{tpt:.3f}"
    trace = f"Root=1-{int(ts.timestamp()):x}-{rnd.getrandbits(96):024x}"
    return (f'https {t} {elb} {cip}:{rnd.randint(1024, 65535)} {tgt if tstatus != "-" else "-"} 0.000 {tpt_s} 0.000 '
            f'{status} {tstatus} {rnd.randint(200, 900)} {sent} "{verb} https://{host}:443{path} HTTP/1.1" "{ua}" '
            f'ECDHE-RSA-AES128-GCM-SHA256 TLSv1.2 arn:aws:elasticloadbalancing:{REGION}:{ACCT}:targetgroup/{tg} '
            f'"{trace}" "{host}" "arn:aws:acm:{REGION}:{ACCT}:certificate/0f6e2a1b-demo" 1 {ct} "forward" "-" "-" '
            f'"{tgt if tstatus != "-" else "-"}" "{tstatus}" "-" "-" TID_{rnd.getrandbits(64):016x} "-" "-" "-"')


def gen_alb():
    stats = {}
    for day in DAYS:
        d0 = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
        lines = {"shop-web": [], "shop-api": []}
        for i in range(4000):  # 常规流量
            ts = d0 + timedelta(seconds=rnd.randint(0, 86399), microseconds=rnd.randint(0, 999999))
            elb = "shop-web" if rnd.random() < 0.55 else "shop-api"
            cip = rnd.choice(CLIENT_POOL)
            path = rnd.choice(WEB_PATHS if elb == "shop-web" else API_PATHS)
            verb = "POST" if path in ("/api/v1/cart", "/api/v1/orders", "/api/v1/login") else "GET"
            status, tpt = 200, rnd.uniform(0.005, 0.25)
            r = rnd.random()
            if r < 0.04: status = 404
            elif r < 0.06: status = 302
            elif r < 0.065: status = 500
            if path == SLOW_PATH: tpt = rnd.uniform(3.2, 9.5)
            ts_ = "-" if status == 404 and False else str(status)
            lines[elb].append((ts, alb_line(ts, elb, cip, verb, path, status, ts_, tpt, rnd.choice(UAS), rnd.randint(300, 40000))))
        if day == BURST_DAY:  # 5xx 爆发：shop-api 目标返回 502/503
            for i in range(300):
                ts = d0 + timedelta(hours=BURST_HOUR, seconds=rnd.randint(0, 899), microseconds=rnd.randint(0, 999999))
                st = 502 if i % 3 else 503
                lines["shop-api"].append((ts, alb_line(ts, "shop-api", rnd.choice(CLIENT_POOL), "GET", "/api/v1/orders", st, "-", 0, rnd.choice(UAS), 272)))
            for i in range(450):  # 扫描器
                ts = d0 + timedelta(hours=3, seconds=rnd.randint(0, 1799), microseconds=rnd.randint(0, 999999))
                lines["shop-web"].append((ts, alb_line(ts, "shop-web", SCANNER_IP, "GET", rnd.choice(SCAN_PATHS), 404, "404", 0.002, "Mozilla/5.0 zgrab/0.x", 150)))
        y, m, dd = day.split("-")
        base = os.path.join(OUT, "alb", "AWSLogs", ACCT, "elasticloadbalancing", REGION, y, m, dd)
        os.makedirs(base, exist_ok=True)
        for elb, rows in lines.items():
            rows.sort()
            name = ELBS[elb][0].replace("/", ".")
            fn = f"{ACCT}_elasticloadbalancing_{REGION}_{name}_{y}{m}{dd}T2355Z_10.100.1.10_demo{elb[-3:]}.log.gz"
            with gzip.open(os.path.join(base, fn), "wt") as f:
                f.write("\n".join(l for _, l in rows) + "\n")
            stats[f"{day}/{elb}"] = len(rows)
    return stats


# ---------------------------------------------------------------- 主机日志
HOSTS = [
    ("i-0eval000000000a01", "web-prod-01", "ubuntu"),
    ("i-0eval000000000b02", "app-prod-02", "rhel"),
    ("i-0eval000000000c03", "db-prod-03", "al2023"),
]


def syslog_ts(dt, os_):
    if os_ == "ubuntu":
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
    return dt.strftime("%b %e %H:%M:%S")


def gen_hostlogs():
    recs = []  # (dt, host_idx, path, logtype, event_time, line)

    def add(day, hidx, path, logtype, ts, msg_fn):
        iid, hn, os_ = HOSTS[hidx]
        recs.append((day, hidx, path, logtype, ts, msg_fn(syslog_ts(ts, os_), hn)))

    auth = {0: "/var/log/auth.log", 1: "/var/log/secure", 2: "journal:sshd"}
    for day in DAYS[:2]:
        d0 = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
        for h in range(3):
            # 正常运维登录：deploy 公钥（每台每天 6 次）
            for k in range(6):
                ts = d0 + timedelta(hours=9 + k, minutes=rnd.randint(0, 59), seconds=rnd.randint(0, 59))
                pid = rnd.randint(2000, 9000)
                add(day, h, auth[h], "auth", ts, lambda t, hn, pid=pid: f"{t} {hn} sshd[{pid}]: Accepted publickey for deploy from 10.100.1.20 port {rnd.randint(40000,60000)} ssh2: ED25519 SHA256:k3Yd...")
                add(day, h, auth[h], "auth", ts + timedelta(seconds=1), lambda t, hn, pid=pid: f"{t} {hn} sshd[{pid}]: pam_unix(sshd:session): session opened for user deploy(uid=1001) by deploy(uid=0)")
            # 正常 sudo
            ts = d0 + timedelta(hours=10, minutes=5 + h)
            add(day, h, auth[h], "auth", ts, lambda t, hn: f"{t} {hn} sudo:   deploy : TTY=pts/0 ; PWD=/home/deploy ; USER=root ; COMMAND=/usr/bin/systemctl restart nginx")
        # 11-01 web-prod-01 遭 45.155.205.233 暴力破解 120 次
        if day == "2026-11-01":
            for k in range(120):
                ts = d0 + timedelta(hours=2, minutes=10, seconds=k * 10)
                user = ["root", "admin", "test", "oracle", "ubuntu"][k % 5]
                inv = "" if user in ("root", "ubuntu") else "invalid user "
                add(day, 0, auth[0], "auth", ts, lambda t, hn, u=user, inv=inv: f"{t} {hn} sshd[{4100+k}]: Failed password for {inv}{u} from 45.155.205.233 port {rnd.randint(30000,65000)} ssh2")
            # btmp 文本（last -f /var/log/btmp 转换结果）
            for k in range(0, 120, 10):
                ts = d0 + timedelta(hours=2, minutes=10, seconds=k * 10)
                add(day, 0, "/var/log/btmp", "login_btmp", ts, lambda t, hn, ts=ts: f"root     ssh:notty    45.155.205.233   {ts.strftime('%Y-%m-%dT%H:%M:%S+00:00')} - {ts.strftime('%Y-%m-%dT%H:%M:%S+00:00')}  (00:00)")
            # nginx 访问日志
            for k in range(200):
                ts = d0 + timedelta(seconds=rnd.randint(0, 86399))
                st = 200 if k % 10 else (404 if k % 20 else 500)
                add(day, 0, "/var/log/nginx/access.log", "nginx_access", ts, lambda t, hn, ts=ts, st=st: f'10.100.1.{rnd.randint(2,30)} - - [{ts.strftime("%d/%b/%Y:%H:%M:%S +0000")}] "GET /product/{rnd.randint(1000,1010)} HTTP/1.1" {st} {rnd.randint(200,5000)} "-" "ELB-HealthChecker/2.0"')
        # 11-02 app-prod-02：185.220.101.7 暴力破解 60 次 → 随后 root 密码登录成功（可疑）→ 新建账号 → 改 crontab → 读 shadow
        if day == "2026-11-02":
            for k in range(60):
                ts = d0 + timedelta(hours=3, minutes=0, seconds=k * 5)
                add(day, 1, auth[1], "auth", ts, lambda t, hn: f"{t} {hn} sshd[{5200+k}]: Failed password for root from 185.220.101.7 port {rnd.randint(30000,65000)} ssh2")
            ts = d0 + timedelta(hours=3, minutes=6)
            add(day, 1, auth[1], "auth", ts, lambda t, hn: f"{t} {hn} sshd[5301]: Accepted password for root from 103.21.244.9 port 51022 ssh2")
            add(day, 1, auth[1], "auth", ts + timedelta(minutes=8), lambda t, hn: f"{t} {hn} useradd[5333]: new user: name=support2, UID=1002, GID=1002, home=/home/support2, shell=/bin/bash, from=/dev/pts/1")
            add(day, 1, auth[1], "auth", ts + timedelta(minutes=9), lambda t, hn: f"{t} {hn} passwd[5340]: pam_unix(passwd:chauthtok): password changed for support2")
            add(day, 1, auth[1], "auth", ts + timedelta(minutes=10), lambda t, hn: f"{t} {hn} sudo:      ops : TTY=pts/1 ; PWD=/root ; USER=root ; COMMAND=/bin/cat /etc/shadow")
            add(day, 1, "/var/log/cron", "cron", ts + timedelta(minutes=12), lambda t, hn: f"{t} {hn} crontab[5351]: (root) REPLACE (root)")
            add(day, 1, "/var/log/cron", "cron", ts + timedelta(minutes=12, seconds=1), lambda t, hn: f"{t} {hn} crontab[5351]: (root) END EDIT (root)")
            ep = int((ts + timedelta(minutes=8)).timestamp())
            add(day, 1, "/var/log/audit/audit.log", "audit", ts + timedelta(minutes=8), lambda t, hn: f'type=ADD_USER msg=audit({ep}.311:8812): pid=5333 uid=0 auid=0 ses=7 msg=\'op=add-user id=1002 exe="/usr/sbin/useradd" hostname={hn} addr=? terminal=pts/1 res=success\'')
            for k in range(5):
                ep2 = int((d0 + timedelta(hours=3, seconds=k * 5)).timestamp())
                add(day, 1, "/var/log/audit/audit.log", "audit", d0 + timedelta(hours=3, seconds=k * 5), lambda t, hn, ep2=ep2: f'type=USER_LOGIN msg=audit({ep2}.120:{8700+k}): pid={5200+k} uid=0 auid=4294967295 ses=4294967295 msg=\'op=login acct="root" exe="/usr/sbin/sshd" hostname=? addr=185.220.101.7 terminal=ssh res=failed\'')
        # 每天的 cron 例行任务
        for h in (1, 2):
            for hr in range(0, 24, 6):
                ts = d0 + timedelta(hours=hr, minutes=1)
                path = "/var/log/cron" if h == 1 else "journal:crond"
                add(day, h, path, "cron", ts, lambda t, hn: f"{t} {hn} CROND[{rnd.randint(1000,9999)}]: (root) CMD (/usr/local/bin/backup.sh)")

    out = os.path.join(OUT, "hostlogs")
    by_day = {}
    for day, hidx, path, logtype, ts, line in sorted(recs, key=lambda r: (r[0], r[1], r[2], r[4])):
        by_day.setdefault((day, hidx), []).append((path, logtype, ts, line))
    for (day, hidx), rows in by_day.items():
        iid, hn, os_ = HOSTS[hidx]
        d = os.path.join(out, f"dt={day}")
        os.makedirs(d, exist_ok=True)
        counters = {}
        with gzip.open(os.path.join(d, f"{REGION}_{iid}_synthetic.json.gz"), "wt") as f:
            for path, logtype, ts, line in rows:
                counters[path] = counters.get(path, 0) + 1
                f.write(json.dumps({
                    "region": REGION, "instance_id": iid, "host_name": hn, "path": path, "logtype": logtype,
                    "line_no": counters[path], "event_time": ts.strftime("%Y-%m-%d %H:%M:%S"), "line": line,
                    "sync_id": f"synthetic-{day}", "collected_at": f"{day} 23:30:00"}, ensure_ascii=False) + "\n")
    return len(recs)


if __name__ == "__main__":
    s = gen_alb()
    n = gen_hostlogs()
    print(json.dumps({"alb": s, "hostlog_lines": n}, indent=1))
