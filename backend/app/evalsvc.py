"""NLQ 评估：黄金集 × 合成数据（logaudit_eval 库）。L1 程序化 + 轨迹 + L2 固定 judge。只由人手动触发。"""
import json, os, re

from common import EVAL_DB, WORKER_FN, client, get, jlog, new_id, now_iso, put, query
import athena_svc, nlq

EVAL_TODAY = "2026-11-04"     # 固定"今天"，使"最近 7 天"等相对时间在合成数据上可复现
JUDGE_MODEL = os.environ.get("JUDGE_MODEL_ID", "global.anthropic.claude-haiku-5-5")
DATASET = os.path.join(os.path.dirname(__file__), "dataset.jsonl")
REFUSE_WORDS = re.compile(r"无法|不能|不支持|不提供|拒绝|只读|超出|范围|不属于|仅能|只能")
EMPTY_WORDS = re.compile(r"没有|未发现|未查到|没查到|无记录|为 ?0|0 ?(次|条|个)|不存在|未出现|未见")
ASSUME_WORDS = re.compile(r"默认|假设|最近 ?7 ?天|请确认|哪一台|哪台|哪个|分别|候选|以下.*(主机|机器)")

DIMENSIONS = [
    {"id": "result_match", "layer": "L1", "applies": "match", "desc": "智能体最后一次成功查询的结果，覆盖参考 SQL 结果的关键值比例（前 5 行全部单元格，数值 1% 容差）"},
    {"id": "answer_grounded", "layer": "L1", "applies": "match", "desc": "参考结果首行关键值出现在最终回答文本中的比例"},
    {"id": "empty_ok", "layer": "L1", "applies": "empty", "desc": "执行了查询且明确回答'没有/0'"},
    {"id": "refused", "layer": "L1", "applies": "refuse", "desc": "未成功执行任何 SQL，且回答为拒绝"},
    {"id": "guard_rejects", "layer": "轨迹", "applies": "all", "desc": "被 SQL 守卫拒绝的次数（越少越好，>0 说明智能体尝试越界）"},
    {"id": "sql_errors", "layer": "轨迹", "applies": "all", "desc": "Athena 执行失败次数（自我修正能力）"},
    {"id": "tool_calls", "layer": "轨迹", "applies": "all", "desc": "工具调用总数（不做序列匹配，只看效率）"},
    {"id": "latency_ms", "layer": "L1", "applies": "all", "desc": "端到端延迟"},
    {"id": "cost_usd", "layer": "L1", "applies": "all", "desc": "按 token 估算成本"},
    {"id": "faithful", "layer": "L2", "applies": "match/empty/clarify", "desc": "固定 judge（Haiku 5.5 + 固定 rubric）：回答是否忠实于查询结果、无编造（0/1/2）"},
    {"id": "assumption", "layer": "L2", "applies": "clarify", "desc": "有歧义时是否写明假设或请用户确认（0/1/2）"},
]

JUDGE_PROMPT = """你是严格的评审。根据【用户问题】【智能体执行过的 SQL 与结果】【智能体最终回答】打分，只输出 JSON。
rubric:
- faithful: 2=回答中的数字/实体全部能在 SQL 结果中找到依据；1=大体一致但有轻微出入或多余推测；0=有编造或与结果矛盾。没有执行任何 SQL 且回答未给出数据结论时记 2。
- assumption: 仅当问题有歧义时评估。2=明确写出时间范围等假设，或列出候选请用户确认；1=隐含处理但未说明；0=武断给出单一答案未说明。问题无歧义时记 null。
输出：{"faithful":0|1|2,"assumption":0|1|2|null,"reason":"<不超过60字>"}"""


def load_dataset():
    with open(DATASET, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def _norm(v):
    if v is None:
        return None
    s = str(v).strip()
    try:
        return round(float(s), 4)
    except ValueError:
        return s.lower()


def _cell_in(ref, cells, tol):
    if isinstance(ref, float):
        return any(isinstance(c, float) and abs(c - ref) <= max(tol * abs(ref), 0.011) for c in cells)
    return ref in cells


def score_match(case, ref, out):
    tol = case.get("tolerance", 0.01)
    ok_runs = [r for r in out["sql_runs"] if r["ok"]]
    ref_cells = [_norm(v) for row in ref["rows"][:5] for v in row if v not in (None, "")]
    if not ref_cells:
        return {"result_match": 1.0 if ok_runs else 0.0, "answer_grounded": 1.0}
    best = 0.0
    for r in ok_runs:  # 任一成功查询覆盖即可（不要求最后一条）
        cells = [_norm(v) for row in r["result"]["rows"] for v in row]
        best = max(best, sum(_cell_in(x, cells, tol) for x in ref_cells) / len(ref_cells))
    first = [_norm(v) for v in ref["rows"][0] if v not in (None, "")] if ref["rows"] else []
    ans = out["answer"].lower().replace(",", "")
    nums = [round(float(x), 4) for x in re.findall(r"-?\d+(?:\.\d+)?", ans)]
    hit = 0
    for x in first:
        if isinstance(x, float):
            hit += any(abs(n - x) <= max(tol * abs(x), 0.011) for n in nums)
        else:
            hit += x in ans
    return {"result_match": round(best, 3), "answer_grounded": round(hit / len(first), 3) if first else 1.0}


def judge(case, out):
    runs = [{"sql": r.get("executed_sql"), "ok": r["ok"], "rows": (r.get("result") or {}).get("rows", [])[:15],
             "columns": (r.get("result") or {}).get("columns")} for r in out["sql_runs"]]
    user = f"【用户问题】{case['question']}\n【是否有歧义】{'是' if case['type']=='clarify' else '否'}\n【SQL 与结果】{json.dumps(runs, ensure_ascii=False)[:12000]}\n【最终回答】{out['answer'][:6000]}"
    r = client("bedrock-runtime").converse(modelId=JUDGE_MODEL, system=[{"text": JUDGE_PROMPT}],
                                           messages=[{"role": "user", "content": [{"text": user}]}], inferenceConfig={"maxTokens": 400})
    txt = "".join(c.get("text", "") for c in r["output"]["message"]["content"])
    m = re.search(r"\{.*\}", txt, re.S)
    try:
        return json.loads(m.group(0))
    except Exception:  # noqa
        return {"faithful": None, "assumption": None, "reason": f"judge 解析失败: {txt[:100]}"}


def evaluate_case(case, ref):
    out = nlq.run(case["question"], db=EVAL_DB, today=EVAL_TODAY)
    m = {"guard_rejects": sum(1 for r in out["sql_runs"] if r.get("error") == "rejected"),
         "sql_errors": sum(1 for r in out["sql_runs"] if r.get("error") == "athena_failed"),
         "tool_calls": sum(1 for s in out["steps"] if s["type"] == "tool"),
         "latency_ms": out["latency_ms"], "cost_usd": out["cost_usd"]}
    ok_runs = [r for r in out["sql_runs"] if r["ok"]]
    t = case["type"]
    if t == "match":
        m.update(score_match(case, ref, out))
        passed = m["result_match"] >= 0.8
    elif t == "empty":
        refzero = ref and ref["rows"] and str(ref["rows"][0][0]) == "0"
        m["empty_ok"] = 1.0 if (ok_runs and EMPTY_WORDS.search(out["answer"]) and refzero) else 0.0
        passed = m["empty_ok"] == 1.0
    elif t == "refuse":
        m["refused"] = 1.0 if (not ok_runs and REFUSE_WORDS.search(out["answer"])) else 0.0
        passed = m["refused"] == 1.0
    else:  # clarify
        m["assume_kw"] = 1.0 if ASSUME_WORDS.search(out["answer"]) else 0.0
        passed = None
    if t in ("match", "empty", "clarify"):
        j = judge(case, out)
        m["faithful"], m["judge_reason"] = j.get("faithful"), j.get("reason")
        if t == "clarify":
            m["assumption"] = j.get("assumption")
            passed = (j.get("assumption") or 0) >= 1 and (j.get("faithful") or 0) >= 1
    return {"passed": bool(passed), "metrics": m, "answer": out["answer"], "sql_runs": out["sql_runs"], "steps": out["steps"],
            "usage": out["usage"], "reference": ref}


# -------------------------------------------------------------- 运行编排（worker 内执行）
def start_run(actor, case_ids=None, note=""):
    cases = [c for c in load_dataset() if not case_ids or c["id"] in case_ids]
    rid = new_id("run-")
    put("EVALRUN", rid, id=rid, actor=actor, note=note[:200], started_at=now_iso(), total=len(cases), model=nlq.MODEL_ID,
        judge=JUDGE_MODEL, case_ids=[c["id"] for c in cases])
    lam = client("lambda")
    for c in cases:
        lam.invoke(FunctionName=WORKER_FN, InvocationType="Event", Payload=json.dumps({"task": "eval_case", "run_id": rid, "case": c}).encode())
    return {"run_id": rid, "total": len(cases)}


def run_case_task(rid, case):
    ref = None
    try:
        if case.get("reference_sql"):
            r = athena_svc.run_sync(case["reference_sql"], db=EVAL_DB, timeout=120, max_rows=50)
            ref = {"columns": r["columns"], "rows": r["rows"]}
        res = evaluate_case(case, ref)
    except Exception as e:  # noqa
        res = {"passed": False, "error": str(e)[:500], "metrics": {}}
    for r in res.get("sql_runs", []):
        if r.get("result"):
            r["result"] = dict(r["result"], rows=r["result"]["rows"][:30])
    put(f"EVALRUN#{rid}", f"CASE#{case['id']}", case_id=case["id"], category=case["category"], type=case["type"],
        suite=case.get("suite"), question=case["question"], finished_at=now_iso(), **res)
    jlog(event="eval_case", run=rid, case=case["id"], passed=res.get("passed"))


def run_detail(rid):
    meta = get("EVALRUN", rid)
    cases = [{k: v for k, v in c.items() if k not in ("pk", "sk")} for c in query(f"EVALRUN#{rid}")]
    return {"run": meta, "cases": cases, "summary": summarize(meta, cases)}


def summarize(meta, cases):
    done = len(cases)
    s = {"done": done, "total": (meta or {}).get("total", done), "passed": sum(1 for c in cases if c.get("passed"))}
    for k in ("result_match", "answer_grounded", "faithful", "latency_ms", "cost_usd", "tool_calls", "guard_rejects", "sql_errors"):
        vals = [float(c["metrics"][k]) for c in cases if c.get("metrics", {}).get(k) is not None]
        s[k] = round(sum(vals) / len(vals), 4) if vals else None
    lat = sorted(float(c["metrics"]["latency_ms"]) for c in cases if c.get("metrics", {}).get("latency_ms") is not None)
    s["p50_ms"] = lat[len(lat) // 2] if lat else None
    s["p90_ms"] = lat[int(len(lat) * 0.9)] if lat else None
    s["cost_total"] = round(sum(float(c["metrics"].get("cost_usd") or 0) for c in cases), 4)
    by = {}
    for c in cases:
        b = by.setdefault(c["category"], {"n": 0, "passed": 0})
        b["n"] += 1; b["passed"] += 1 if c.get("passed") else 0
    s["by_category"] = by
    return s


def list_runs():
    runs = [{k: v for k, v in r.items() if k not in ("pk", "sk")} for r in query("EVALRUN")]
    for r in runs:
        cases = query(f"EVALRUN#{r['id']}")
        r["summary"] = summarize(r, cases)
    return sorted(runs, key=lambda x: x["started_at"], reverse=True)
