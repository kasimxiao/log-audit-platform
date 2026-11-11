#!/usr/bin/env python3
"""校验评估集参考 SQL 在 logaudit_eval 上可执行（ground truth 自检，不调用模型）。"""
import json, os, sys
from concurrent.futures import ThreadPoolExecutor
import boto3
REGION = os.environ.get("LA_REGION", "ap-northeast-1"); os.environ["AWS_REGION"] = REGION
os.environ.setdefault("ATHENA_WORKGROUP", "logaudit")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "app"))
import athena_svc
cases = [json.loads(l) for l in open(os.path.join(os.path.dirname(__file__), "..", "evals", "dataset.jsonl")) if l.strip()]
def run(c):
    try: r = athena_svc.run_sync(c["reference_sql"], db="logaudit_eval", timeout=120, max_rows=6); return c["id"], r["rows"][:5]
    except Exception as e: return c["id"], f"ERROR {getattr(e,'msg',e)}"
with ThreadPoolExecutor(8) as ex:
    for cid, rows in ex.map(run, [c for c in cases if c.get("reference_sql")]): print(cid, json.dumps(rows, ensure_ascii=False)[:230])
