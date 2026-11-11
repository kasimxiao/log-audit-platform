import sys, time, os; sys.path.insert(0, os.path.dirname(__file__))
from api_client import call
R = os.environ.get("LA_REGION", "ap-northeast-1")
hosts = {h["name"]: h for h in call("GET", f"/api/hosts?region={R}&tag_key=Project&tag_value=log-audit-poc")["hosts"]}
print({n: (h["instance_id"], h["ssm"]) for n, h in hosts.items()})
plan = {"la-web-01": ["linux-security", "web-nginx"], "la-ubuntu-02": ["ubuntu-security", "web-httpd"], "la-legacy-03": ["al2023-journal", "web-nginx"]}
for n, tpls in plan.items():
    h = hosts[n]; call("PUT", f"/api/hosts/{R}/{h['instance_id']}/config", {"enabled": True, "templates": tpls, "extra_paths": [], "name": n})
res = call("POST", "/api/sync/run", {"targets": [{"region": R, "instance_id": hosts[n]["instance_id"]} for n in plan]})
print([(r["name"], r["status"], r.get("message")) for r in res["results"]])
day = time.strftime("%Y-%m-%d", time.gmtime())
for i in range(40):
    time.sleep(6); items = call("GET", f"/api/sync/history?day={day}")["items"]
    st = [(x["name"], x["status"]) for x in items if x["sync_id"] in {r["sync_id"] for r in res["results"]}]
    if all(s in ("ingested", "host_failed", "dispatch_failed") for _, s in st): break
for x in items:
    if x["sync_id"] in {r["sync_id"] for r in res["results"]}:
        print(x["name"], x["status"], x.get("ingest"), (x.get("host_output") or "")[-200:], (x.get("host_error") or "")[-300:])
