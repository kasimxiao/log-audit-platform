"""异步任务：智能问答、评估用例、每日同步调度入口。"""
from common import now_iso, put, get, table, to_ddb, jlog
import evalsvc, nlq


def handler(event, _ctx):
    task = event.get("task")
    if task == "nlq":
        jid = event["job_id"]

        def on_step(steps):
            table().update_item(Key={"pk": "JOB", "sk": jid}, UpdateExpression="SET steps=:s",
                                ExpressionAttributeValues={":s": to_ddb(steps)})
        try:
            out = nlq.run(event["question"], history=event.get("history"), on_step=on_step)
            table().update_item(Key={"pk": "JOB", "sk": jid},
                                UpdateExpression="SET #s=:s, answer=:a, steps=:st, sql_runs=:r, usage_=:u, latency_ms=:l, cost_usd=:c, model=:m, finished_at=:f",
                                ExpressionAttributeNames={"#s": "status"},
                                ExpressionAttributeValues=to_ddb({":s": "done", ":a": out["answer"], ":st": out["steps"],
                                                                  ":r": [dict(r, result=_slim(r.get("result"))) for r in out["sql_runs"]],
                                                                  ":u": out["usage"], ":l": out["latency_ms"], ":c": out["cost_usd"],
                                                                  ":m": out["model"], ":f": now_iso()}))
        except Exception as e:  # noqa
            jlog(level="error", task="nlq", err=repr(e)[:800])
            table().update_item(Key={"pk": "JOB", "sk": jid}, UpdateExpression="SET #s=:s, #e=:e",
                                ExpressionAttributeNames={"#s": "status", "#e": "error"},
                                ExpressionAttributeValues={":s": "error", ":e": str(e)[:500]})
    elif task == "eval_case":
        evalsvc.run_case_task(event["run_id"], event["case"])
    elif task == "alerts":
        import alerts
        return alerts.run(actor=event.get("actor", "scheduler"))
    return {"ok": True}


def _slim(r):
    if not r:
        return r
    return dict(r, rows=r["rows"][:50])
