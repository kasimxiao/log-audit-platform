#!/usr/bin/env python3
"""手动触发一轮 NLQ 评估（与评估控制台"运行评估"等价），并等待完成后打印汇总。
用法：[AWS_PROFILE=<你的 profile>] [LA_REGION=<区域>] python3 evals/run_eval.py [--note "说明"] [--cases A01,H03] [--no-wait]"""
import argparse, json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from api_client import call
ap = argparse.ArgumentParser(); ap.add_argument("--note", default=""); ap.add_argument("--cases", default=""); ap.add_argument("--no-wait", action="store_true")
a = ap.parse_args()
r = call("POST", "/api/eval/runs", {"note": a.note, "case_ids": [c for c in a.cases.split(",") if c] or None})
print("run:", r["run_id"], "cases:", r["total"])
if a.no_wait: sys.exit(0)
while True:
    time.sleep(15); d = call("GET", f"/api/eval/runs/{r['run_id']}"); s = d["summary"]
    print(f"  {s['done']}/{s['total']} passed={s['passed']}")
    if s["done"] >= s["total"]: break
print(json.dumps(s, ensure_ascii=False, indent=1))
