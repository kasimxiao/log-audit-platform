"""HTTP API 入口（API Gateway HTTP API + Cognito JWT）。路由表声明角色要求与审计动作。"""
import json, re, time
from datetime import date, timedelta

from common import ApiError, GLUE_DB, WORKER_FN, client, dumps, get, jlog, new_id, now_iso, put, resp
import alb, athena_svc, cloudtrail, evalsvc, hosts, livequery, scenarios, syncsvc, users

ROUTES = []


def route(method, pattern, role="user", audit=None):
    rx = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

    def deco(fn):
        ROUTES.append((method, rx, role, audit, fn))
        return fn
    return deco


# ------------------------------------------------------------------ 身份
@route("GET", "/api/me")
def me(ctx, **_):
    return {"username": ctx["user"], "role": ctx["role"], "name": ctx.get("name")}


# ------------------------------------------------------------------ 主机
@route("GET", "/api/regions")
def regions(ctx, **_):
    return {"regions": hosts.region_summary()}


@route("GET", "/api/hosts")
def list_hosts(ctx, q, **_):
    items = hosts.list_hosts(q.get("region"), q.get("tag_key"), q.get("tag_value"))
    return {"hosts": items, "hints": hosts.SSM_HINT}


@route("POST", "/api/hosts/{region}/{iid}/onboard", role="admin", audit="host.onboard")
def onboard(ctx, region, iid, **_):
    return hosts.onboard(region, iid, ctx["user"])


@route("GET", "/api/hosts/{region}/{iid}/config")
def host_cfg(ctx, region, iid, **_):
    c = hosts.get_cfg(region, iid)
    return {**{k: v for k, v in c.items() if k not in ("pk", "sk")}, "effective_paths": syncsvc.effective_paths(c)}


@route("PUT", "/api/hosts/{region}/{iid}/config", role="admin", audit="host.config")
def put_host_cfg(ctx, region, iid, body, **_):
    return hosts.set_cfg(region, iid, body, ctx["user"])


@route("POST", "/api/hosts/{region}/enroll-by-tag", role="admin", audit="host.enroll_by_tag")
def enroll(ctx, region, body, **_):
    return hosts.bulk_enroll(region, body, ctx["user"])


@route("POST", "/api/hosts/{region}/tags", role="admin", audit="host.tag")
def tag(ctx, region, body, **_):
    return hosts.tag_hosts(region, body.get("instance_ids") or [], body.get("key"), body.get("value"), ctx["user"])


# ------------------------------------------------------------------ 采集模板 / 同步
@route("GET", "/api/templates")
def templates(ctx, **_):
    return {"templates": syncsvc.list_templates()}


@route("PUT", "/api/templates", role="admin", audit="template.save")
def save_tpl(ctx, body, **_):
    return syncsvc.save_template(body, ctx["user"])


@route("DELETE", "/api/templates/{tid}", role="admin", audit="template.delete")
def del_tpl(ctx, tid, **_):
    return syncsvc.delete_template(tid)


@route("POST", "/api/sync/run", role="admin", audit="sync.run")
def sync_run(ctx, body, **_):
    t = body.get("targets")
    targets = [(x["region"], x["instance_id"]) for x in t] if t else None
    return {"results": syncsvc.dispatch(targets, actor=ctx["user"], trigger="manual")}


@route("GET", "/api/sync/history")
def sync_hist(ctx, q, **_):
    return {"items": syncsvc.history(q.get("day") or date.today().isoformat())}


# ------------------------------------------------------------------ 实时查询
@route("GET", "/api/live/roots")
def live_roots(ctx, **_):
    return {"roots": livequery.allowed_roots()}


@route("PUT", "/api/live/roots", role="admin", audit="live.roots")
def set_live_roots(ctx, body, **_):
    return livequery.set_roots(body.get("roots") or [], ctx["user"])


@route("POST", "/api/live", audit="live.query")
def live_start(ctx, body, **_):
    return livequery.start(body, ctx["user"])


@route("GET", "/api/live/{cid}")
def live_get(ctx, cid, **_):
    return livequery.result(cid, ctx["user"], ctx["role"] == "admin")


# ------------------------------------------------------------------ 历史查询 / 审计场景
@route("POST", "/api/hostlogs/query", audit="hostlogs.query")
def hl_query(ctx, body, **_):
    sql = scenarios.history_sql(body)
    return {"query_id": athena_svc.start(sql, owner=ctx["user"], label="主机日志检索"), "sql": sql}


@route("GET", "/api/scenarios")
def scn_list(ctx, **_):
    return {"scenarios": [{k: v for k, v in s.items() if k != "sql"} | {"sql_template": s["sql"]} for s in scenarios.SCENARIOS]}


@route("POST", "/api/scenarios/{sid}/run", audit="scenario.run")
def scn_run(ctx, sid, body, **_):
    sql = scenarios.scenario_sql(sid, body)
    return {"query_id": athena_svc.start(sql, owner=ctx["user"], label=f"审计场景 {sid}"), "sql": sql}


@route("GET", "/api/query/{qid}")
def q_get(ctx, qid, q, **_):
    out = athena_svc.get_owned(qid, ctx["user"], ctx["role"] == "admin", max_rows=int(q.get("max_rows") or 500))
    if out.get("rows") and q.get("geo") == "1":
        out["columns"], out["rows"], out["countries"] = alb.geo_enrich(out["columns"], out["rows"])
    return out


# ------------------------------------------------------------------ ALB
@route("GET", "/api/alb/sources")
def alb_sources(ctx, **_):
    return {"sources": alb.list_sources()}


@route("GET", "/api/alb/elbs", role="admin")
def alb_elbs(ctx, **_):
    return {"elbs": alb.list_elbs()}


@route("POST", "/api/alb/detect", role="admin")
def alb_detect(ctx, body, **_):
    b, pf = body.get("bucket", "").strip(), body.get("prefix", "").strip()
    if not alb.RE_BUCKET.match(b) or not alb.RE_PREFIX.match(pf):
        raise ApiError(400, "桶名或前缀非法")
    return {"found": alb.detect_with_access(b, pf)}


@route("POST", "/api/alb/sources", role="admin", audit="alb.source_create")
def alb_create(ctx, body, **_):
    return alb.create_source(body, ctx["user"])


@route("DELETE", "/api/alb/sources/{sid}", role="admin", audit="alb.source_delete")
def alb_delete(ctx, sid, **_):
    return alb.delete_source(sid)


@route("GET", "/api/alb/reports")
def alb_reports(ctx, **_):
    return {"reports": [{"id": k, "name": v[0]} for k, v in alb.REPORTS.items()]}


@route("POST", "/api/alb/{sid}/report/{rid}", audit="alb.report")
def alb_report(ctx, sid, rid, body, **_):
    sql = alb.report_sql(sid, rid, body)
    return {"query_id": athena_svc.start(sql, owner=ctx["user"], label=f"ALB {sid}/{rid}"), "sql": sql}


@route("POST", "/api/alb/{sid}/search", audit="alb.search")
def alb_search(ctx, sid, body, **_):
    sql = alb.search_sql(sid, body)
    return {"query_id": athena_svc.start(sql, owner=ctx["user"], label=f"ALB {sid} 检索"), "sql": sql}


# ------------------------------------------------------------------ CloudTrail
@route("GET", "/api/cloudtrail/config")
def ct_cfg(ctx, **_):
    c = cloudtrail.config()
    return {"config": {k: v for k, v in (c or {}).items() if k not in ("pk", "sk")} or None,
            "presets": [{"id": k, "name": v[0]} for k, v in cloudtrail.PRESETS.items()]}


@route("GET", "/api/cloudtrail/trails", role="admin")
def ct_trails(ctx, **_):
    return {"trails": cloudtrail.list_trails()}


@route("PUT", "/api/cloudtrail/config", role="admin", audit="cloudtrail.config")
def ct_set(ctx, body, **_):
    return cloudtrail.configure(body, ctx["user"])


@route("POST", "/api/cloudtrail/preset/{pid}", audit="cloudtrail.preset")
def ct_preset(ctx, pid, body, **_):
    sql = cloudtrail.preset_sql(pid, body)
    return {"query_id": athena_svc.start(sql, owner=ctx["user"], label=f"CloudTrail {pid}"), "sql": sql}


@route("POST", "/api/cloudtrail/search", audit="cloudtrail.search")
def ct_search(ctx, body, **_):
    sql = cloudtrail.search_sql(body)
    return {"query_id": athena_svc.start(sql, owner=ctx["user"], label="CloudTrail 检索"), "sql": sql}


# ------------------------------------------------------------------ 智能问答
@route("POST", "/api/nlq", audit="nlq.ask")
def nlq_ask(ctx, body, **_):
    qn = (body.get("question") or "").strip()
    if not qn or len(qn) > 2000:
        raise ApiError(400, "问题为空或过长")
    jid = new_id("nlq-")
    put("JOB", jid, id=jid, owner=ctx["user"], kind="nlq", status="running", question=qn, created_at=now_iso())
    client("lambda").invoke(FunctionName=WORKER_FN, InvocationType="Event",
                            Payload=json.dumps({"task": "nlq", "job_id": jid, "question": qn,
                                                "history": (body.get("history") or [])[-6:]}).encode())
    return {"job_id": jid}


@route("GET", "/api/nlq/{jid}")
def nlq_get(ctx, jid, **_):
    j = get("JOB", jid)
    if not j or (j["owner"] != ctx["user"] and ctx["role"] != "admin"):
        raise ApiError(404, "任务不存在")
    return {k: v for k, v in j.items() if k not in ("pk", "sk")}


# ------------------------------------------------------------------ 告警
@route("GET", "/api/alerts/history")
def al_hist(ctx, q, **_):
    import alerts
    return {"items": alerts.history(int(q.get("days") or 14))}


@route("GET", "/api/alerts/settings", role="admin")
def al_get(ctx, **_):
    import alerts
    return {"settings": alerts.settings(), "subscriptions": alerts.subscriptions(),
            "scenarios": [{"id": s["id"], "name": s["name"], "level": s["level"]} for s in scenarios.SCENARIOS]}


@route("PUT", "/api/alerts/settings", role="admin", audit="alerts.settings")
def al_put(ctx, body, **_):
    import alerts
    return alerts.save_settings(body, ctx["user"])


@route("POST", "/api/alerts/subscriptions", role="admin", audit="alerts.subscribe")
def al_sub(ctx, body, **_):
    import alerts
    return alerts.subscribe((body.get("email") or "").strip())


@route("POST", "/api/alerts/unsubscribe", role="admin", audit="alerts.unsubscribe")
def al_unsub(ctx, body, **_):
    import alerts
    return alerts.unsubscribe(body.get("arn", ""))


@route("POST", "/api/alerts/run", role="admin", audit="alerts.run")
def al_run(ctx, body, **_):
    import alerts
    if body.get("dry_run"):
        return alerts.run(actor=ctx["user"], dry_run=True)
    client("lambda").invoke(FunctionName=WORKER_FN, InvocationType="Event",
                            Payload=json.dumps({"task": "alerts", "actor": ctx["user"]}).encode())
    return {"ok": True, "message": "已触发，约 1 分钟后在告警记录中查看"}


# ------------------------------------------------------------------ 用户 / 审计 / 评估（admin）
@route("POST", "/api/admin/source-access/sync", role="admin", audit="admin.source_access_sync")
def sa_sync(ctx, **_):
    import source_access
    return source_access.sync()


@route("GET", "/api/users", role="admin")
def u_list(ctx, **_):
    return {"users": users.list_users()}


@route("POST", "/api/users", role="admin", audit="user.create")
def u_create(ctx, body, **_):
    return users.create_user(body)


@route("PUT", "/api/users/{name}/role", role="admin", audit="user.role")
def u_role(ctx, name, body, **_):
    return users.set_role(name, body.get("role"), ctx["user"])


@route("PUT", "/api/users/{name}/enabled", role="admin", audit="user.enable")
def u_enable(ctx, name, body, **_):
    return users.set_enabled(name, bool(body.get("enabled")), ctx["user"])


@route("POST", "/api/users/{name}/reset-password", role="admin", audit="user.reset_password")
def u_reset(ctx, name, **_):
    return users.reset_password(name)


@route("DELETE", "/api/users/{name}", role="admin", audit="user.delete")
def u_del(ctx, name, **_):
    return users.delete_user(name, ctx["user"])


@route("GET", "/api/audit", role="admin")
def audit_list(ctx, q, **_):
    today = date.today()
    return {"items": users.list_audit(q.get("from") or (today - timedelta(days=6)).isoformat(), q.get("to") or today.isoformat(),
                                      q.get("actor"), q.get("action"))}


@route("GET", "/api/eval/dataset", role="admin")
def ev_ds(ctx, **_):
    return {"cases": evalsvc.load_dataset(), "dimensions": evalsvc.DIMENSIONS, "today": evalsvc.EVAL_TODAY,
            "judge_model": evalsvc.JUDGE_MODEL, "judge_prompt": evalsvc.JUDGE_PROMPT}


@route("GET", "/api/eval/runs", role="admin")
def ev_runs(ctx, **_):
    return {"runs": evalsvc.list_runs()}


@route("GET", "/api/eval/runs/{rid}", role="admin")
def ev_run(ctx, rid, **_):
    return evalsvc.run_detail(rid)


@route("POST", "/api/eval/runs", role="admin", audit="eval.run")
def ev_start(ctx, body, **_):
    return evalsvc.start_run(ctx["user"], body.get("case_ids"), body.get("note", ""))


# ------------------------------------------------------------------ 分发
def _ctx(event):
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    groups = claims.get("cognito:groups", "")
    groups = groups if isinstance(groups, list) else re.findall(r"[\w-]+", groups)
    return {"user": claims.get("cognito:username") or claims.get("username") or "?", "name": claims.get("name"),
            "role": "admin" if "admin" in groups else "user"}


def handler(event, _ctx_):
    t0 = time.time()
    method = event["requestContext"]["http"]["method"]
    path = event.get("rawPath", "")
    ip = event["requestContext"]["http"].get("sourceIp", "")
    ctx = _ctx(event)
    q = event.get("queryStringParameters") or {}
    audit_name, target, status, body = None, path, 200, {}
    try:
        body = json.loads(event.get("body") or "{}") if event.get("body") else {}
        if event.get("isBase64Encoded") and event.get("body"):
            import base64
            body = json.loads(base64.b64decode(event["body"]))
        for m, rx, role, audit, fn in ROUTES:
            mt = rx.match(path)
            if m == method and mt:
                audit_name = audit
                if role == "admin" and ctx["role"] != "admin":
                    raise ApiError(403, "需要管理员权限")
                out = fn(ctx, q=q, body=body, **mt.groupdict())
                return resp(200, out)
        raise ApiError(404, "接口不存在")
    except ApiError as e:
        status = e.status
        return resp(e.status, {"error": e.code, "message": e.msg})
    except client("ssm").exceptions.InvalidInstanceId:
        status = 400
        return resp(400, {"error": "invalid_instance", "message": "实例未在 SSM 中在线（未纳管或 Agent 离线）"})
    except Exception as e:  # noqa
        status = 500
        jlog(level="error", path=path, err=repr(e)[:1000])
        return resp(500, {"error": "internal", "message": str(e)[:300]})
    finally:
        if audit_name:
            try:
                detail = dumps({k: v for k, v in (body if isinstance(body, dict) else {}).items() if k not in ("password",)})
                users.record(ctx["user"], ctx["role"], audit_name, target, detail, status, ip, int((time.time() - t0) * 1000))
            except Exception as e:  # noqa
                jlog(level="error", where="audit", err=repr(e)[:300])
