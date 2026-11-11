#!/usr/bin/env python3
"""主机日志一致性指纹：各库按 logtype 行数 + 全行校验和 + 审计场景结果 + 评估集参考 SQL 结果（迁移前后对比用）。"""
import json, os, sys
os.environ.update(AWS_REGION=os.environ.get("LA_REGION", "ap-northeast-1"), ATHENA_WORKGROUP="logaudit")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "app"))
import athena_svc, scenarios
out = {}
for db, w in [("logaudit", "dt >= '2026-11-11'"), ("logaudit_eval", "dt >= '2026-11-01'")]:
    out[db + ".by_logtype"] = athena_svc.run_sync(f"SELECT logtype, count(*), checksum(concat_ws('|', instance_id, host_name, path, cast(line_no as varchar), coalesce(event_time,''), line)) FROM hostlogs WHERE {w} GROUP BY 1 ORDER BY 1", db=db, max_rows=100)["rows"]
for sid in ["ssh_bruteforce", "root_login", "sudo_exec", "account_change", "cron_change", "audit_login_failed", "btmp_failed", "web_errors", "success_after_fail"]:
    for db, f in [("logaudit", {"date_from": "2026-11-11"}), ("logaudit_eval", {"date_from": "2026-11-01", "date_to": "2026-11-02"})]:
        r = athena_svc.run_sync(scenarios.scenario_sql(sid, f), db=db, max_rows=500)["rows"]
        out[f"{db}.{sid}"] = sorted(r, key=lambda x: [str(v) for v in x])
cases = [json.loads(l) for l in open(os.path.join(os.path.dirname(__file__), "..", "evals", "dataset.jsonl")) if l.strip()]
for c in cases:
    if c.get("reference_sql") and "hostlogs" in c["reference_sql"]:
        out["eval_ref." + c["id"]] = sorted(athena_svc.run_sync(c["reference_sql"], db="logaudit_eval", max_rows=50)["rows"], key=lambda x: [str(v) for v in x])
json.dump(out, open(sys.argv[1], "w"), ensure_ascii=False, indent=1)
print("saved", sys.argv[1], len(out), "items")
