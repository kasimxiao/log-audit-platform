import sys, time, os, json; sys.path.insert(0, os.path.dirname(__file__))
from api_client import call, query
R = os.environ.get("LA_REGION", "ap-northeast-1"); D = time.strftime("%Y-%m-%d", time.gmtime())
def show(name, r):
    if r.get("state") != "SUCCEEDED": print("✗", name, r.get("state"), r.get("reason")); return
    print("✓", name, f"{len(r['rows'])} 行", json.dumps(r["rows"][:3], ensure_ascii=False)[:220])
show("场景 ssh_bruteforce", query(call("POST", "/api/scenarios/ssh_bruteforce/run", {"date_from": D, "threshold": 3})))
show("场景 account_change", query(call("POST", "/api/scenarios/account_change/run", {"date_from": D})))
show("场景 btmp_failed", query(call("POST", "/api/scenarios/btmp_failed/run", {"date_from": D})))
show("历史检索 Failed password", query(call("POST", "/api/hostlogs/query", {"date_from": D, "keyword": "Failed password", "limit": 5})))
for rid in ["overview", "trend", "top_ip", "scan", "geo"]:
    show(f"ALB demo/{rid}", query(call("POST", f"/api/alb/demo/report/{rid}", {"date_from": "2026-11-01", "date_to": "2026-11-03"})))
show("ALB shop_web/overview(实时测试环境)", query(call("POST", "/api/alb/shop_web/report/overview", {"date_from": D})))
show("ALB shop_api/status", query(call("POST", "/api/alb/shop_api/report/status", {"date_from": D})))
show("CloudTrail console_login", query(call("POST", "/api/cloudtrail/preset/console_login", {"date_from": D, "regions": ["us-east-1"]})))
show("CloudTrail ssm_sessions", query(call("POST", "/api/cloudtrail/preset/ssm_sessions", {"date_from": D, "regions": [R]})))
hosts = {h["name"]: h["instance_id"] for h in call("GET", f"/api/hosts?region={R}&tag_key=Project")["hosts"]}
for name, body in [("ls /var/log", {"mode": "ls", "path": "/var/log/"}), ("grep auth.log", {"mode": "grep", "path": "/var/log/auth.log", "pattern": "Failed password|Invalid user", "lines": 5}),
                   ("last btmp", {"mode": "last", "path": "/var/log/btmp", "lines": 5}), ("越权 /etc/shadow", {"mode": "tail", "path": "/etc/shadow"})]:
    try:
        c = call("POST", "/api/live", {"region": R, "instance_id": hosts["la-ubuntu-02"], **body})
        for _ in range(30):
            time.sleep(1.5); r = call("GET", f"/api/live/{c['command_id']}")
            if r["status"] not in ("Pending", "InProgress", "Delayed"): break
        print("✓ 实时", name, r["status"], (r.get("text") or json.dumps(r.get("entries", [])[:3]) or r.get("error"))[:200].replace("\n", " | "))
    except Exception as e: print("✓(预期拒绝)" if "shadow" in name else "✗", "实时", name, str(e)[:150])
