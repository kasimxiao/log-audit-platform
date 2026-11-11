"""告警：定时（或手动）执行选定审计场景 → 与已告警指纹比对只取新增 → SNS 邮件 + Webhook（飞书/钉钉/企业微信/通用）。"""
import hashlib, ipaddress, json, os, socket, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

from common import ApiError, client, get, jlog, new_id, now_iso, put, query
import athena_svc, scenarios

TOPIC = os.environ.get("ALERT_TOPIC_ARN", "")
DEFAULT = {"enabled": True, "lookback_days": 1, "webhook": {"type": "none", "url": ""},
           "scenarios": {"success_after_fail": {"enabled": True, "threshold": 10}, "ssh_bruteforce": {"enabled": True, "threshold": 50},
                         "root_login": {"enabled": True}, "account_change": {"enabled": True}, "cron_change": {"enabled": False},
                         "sudo_exec": {"enabled": False}, "audit_login_failed": {"enabled": False}, "btmp_failed": {"enabled": False},
                         "web_errors": {"enabled": False}, "ssh_success": {"enabled": False}}}
# 去重键：同一键只告警一次（计数增长不重复告警）
KEYS = {"ssh_bruteforce": ["instance_id", "src_ip", "first_seen"], "success_after_fail": ["instance_id", "success_time", "src_ip"],
        "btmp_failed": ["host_name", "user", "src"], "audit_login_failed": ["host_name", "acct", "addr"],
        "web_errors": ["host_name", "status", "url"]}
WEBHOOK_TYPES = ("none", "feishu", "dingtalk", "wecom", "generic")


def settings():
    s = get("SETTINGS", "alerts") or {}
    out = json.loads(json.dumps(DEFAULT))
    out.update({k: v for k, v in s.items() if k in ("enabled", "lookback_days", "webhook")})
    for sid, v in (s.get("scenarios") or {}).items():
        if sid in out["scenarios"]:
            out["scenarios"][sid].update(v)
    # DynamoDB 读回的数字是 Decimal：统一转成 int / bool，避免 timedelta 等报错
    out["enabled"] = bool(out["enabled"]); out["lookback_days"] = int(out["lookback_days"])
    for v in out["scenarios"].values():
        v["enabled"] = bool(v.get("enabled"))
        if "threshold" in v:
            v["threshold"] = int(v["threshold"])
    return out


def _check_webhook(url):
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or not u.hostname:
        raise ApiError(400, "Webhook 仅支持 https")
    try:
        ips = {ai[4][0] for ai in socket.getaddrinfo(u.hostname, 443)}
    except socket.gaierror:
        raise ApiError(400, "Webhook 域名无法解析")
    for ip in ips:  # 防 SSRF：禁止指向内网/本机/元数据地址
        a = ipaddress.ip_address(ip)
        if a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast or a.is_unspecified:
            raise ApiError(400, f"Webhook 不允许指向内网地址 {ip}")


def save_settings(body, actor):
    cur = settings()
    if "enabled" in body:
        cur["enabled"] = bool(body["enabled"])
    if "lookback_days" in body:
        cur["lookback_days"] = max(1, min(int(body["lookback_days"]), 7))
    if "webhook" in body:
        w = body["webhook"] or {}
        t = w.get("type", "none")
        if t not in WEBHOOK_TYPES:
            raise ApiError(400, "Webhook 类型非法")
        url = (w.get("url") or "").strip()
        if t != "none":
            _check_webhook(url)
        cur["webhook"] = {"type": t, "url": url if t != "none" else ""}
    for sid, v in (body.get("scenarios") or {}).items():
        if sid in cur["scenarios"]:
            cur["scenarios"][sid]["enabled"] = bool(v.get("enabled"))
            if "threshold" in v and "threshold" in cur["scenarios"][sid]:
                cur["scenarios"][sid]["threshold"] = max(1, min(int(v["threshold"]), 100000))
    put("SETTINGS", "alerts", **cur, updated_by=actor, updated_at=now_iso())
    return cur


def subscriptions():
    if not TOPIC:
        return []
    out = []
    for pg in client("sns").get_paginator("list_subscriptions_by_topic").paginate(TopicArn=TOPIC):
        for s in pg["Subscriptions"]:
            if s["Protocol"] == "email":
                out.append({"email": s["Endpoint"], "arn": s["SubscriptionArn"],
                            "status": "待确认（请到邮箱点击确认链接）" if s["SubscriptionArn"] == "PendingConfirmation" else "已确认"})
    return out


def subscribe(email):
    import re
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or ""):
        raise ApiError(400, "邮箱格式不正确")
    client("sns").subscribe(TopicArn=TOPIC, Protocol="email", Endpoint=email)
    return {"ok": True, "message": f"已向 {email} 发送确认邮件，确认后生效"}


def unsubscribe(arn):
    if not arn.startswith(TOPIC + ":"):
        raise ApiError(400, "订阅不存在")
    client("sns").unsubscribe(SubscriptionArn=arn)
    return {"ok": True}


def _fp(sid, cols, row):
    keys = KEYS.get(sid)
    vals = [row[cols.index(k)] for k in keys if k in cols] if keys else row
    return hashlib.sha1(json.dumps([sid, vals], ensure_ascii=False).encode()).hexdigest()[:16]


def run(actor="scheduler", dry_run=False):
    cfg = settings()
    if not cfg["enabled"] and actor == "scheduler":
        return {"skipped": "告警未启用"}
    today = datetime.now(timezone.utc).date()
    f = {"date_from": (today - timedelta(days=cfg["lookback_days"])).isoformat(), "date_to": today.isoformat()}
    found = []
    for sid, sc in cfg["scenarios"].items():
        if not sc.get("enabled"):
            continue
        meta = scenarios.SCN[sid]
        sql = scenarios.scenario_sql(sid, {**f, **({"threshold": sc["threshold"]} if "threshold" in sc else {})})
        try:
            r = athena_svc.run_sync(sql, timeout=180, max_rows=500)
        except Exception as e:  # noqa
            found.append({"scenario": sid, "name": meta["name"], "error": str(e)[:300]}); continue
        seen_item = get("ALERTSEEN", sid) or {}
        seen = set(seen_item.get("fps", []))
        new = [(row, _fp(sid, r["columns"], row)) for row in r["rows"]]
        new = [(row, fp) for row, fp in new if fp not in seen]
        if new:
            found.append({"scenario": sid, "name": meta["name"], "level": meta["level"], "columns": r["columns"],
                          "rows": [row for row, _ in new][:20], "new_count": len(new), "total_hits": len(r["rows"]), "sql": sql})
            if not dry_run:
                fps = list(seen | {fp for _, fp in new})[-5000:]
                put("ALERTSEEN", sid, fps=fps, updated_at=now_iso())
    hits = [x for x in found if x.get("new_count")]
    rec = {"id": new_id("al-"), "at": now_iso(), "actor": actor, "range": f, "hits": hits,
           "errors": [x for x in found if x.get("error")], "notified": []}
    if hits and not dry_run:
        text = render_text(hits, f)
        if TOPIC:
            try:
                confirmed = [x for x in subscriptions() if x["arn"] != "PendingConfirmation"]
                if confirmed:
                    client("sns").publish(TopicArn=TOPIC, Subject=f"[日志审计告警] {len(hits)} 类新增风险"[:100], Message=text)
                    rec["notified"].append(f"email(SNS) ×{len(confirmed)}")
                else:
                    rec["notify_skipped"] = "没有已确认的邮件订阅，未发送邮件"
            except Exception as e:  # noqa
                rec["notify_error"] = f"SNS: {str(e)[:200]}"
        w = cfg["webhook"]
        if w.get("type") not in (None, "none") and w.get("url"):
            try:
                post_webhook(w["type"], w["url"], text, hits)
                rec["notified"].append(f"webhook({w['type']})")
            except Exception as e:  # noqa
                rec["notify_error"] = (rec.get("notify_error", "") + f" Webhook: {str(e)[:200]}").strip()
    if not dry_run:
        put(f"ALERT#{rec['at'][:10]}", rec["at"] + "#" + rec["id"], **rec)
    jlog(event="alerts_run", hits=len(hits), actor=actor)
    return rec


LEVEL_CN = {"critical": "严重", "high": "高", "medium": "中", "low": "低", "info": "信息"}


def render_text(hits, f):
    lines = [f"日志审计平台：{f['date_from']} ~ {f['date_to']}（同步日期）发现 {len(hits)} 类新增风险。", ""]
    for h in hits:
        lines.append(f"【{LEVEL_CN.get(h['level'], h['level'])}】{h['name']}：新增 {h['new_count']} 条")
        for row in h["rows"][:5]:
            lines.append("  · " + " | ".join(f"{c}={v}" for c, v in zip(h["columns"], row) if c not in ("line",) and v not in (None, ""))[:300])
        if h["new_count"] > 5:
            lines.append(f"  … 其余 {h['new_count'] - 5} 条请登录平台查看「告警中心」")
        lines.append("")
    return "\n".join(lines)


def post_webhook(kind, url, text, hits):
    _check_webhook(url)
    body = {"feishu": {"msg_type": "text", "content": {"text": text}},
            "dingtalk": {"msgtype": "text", "text": {"content": text}},
            "wecom": {"msgtype": "text", "text": {"content": text[:2000]}},
            "generic": {"title": "日志审计告警", "text": text, "alerts": [{k: h[k] for k in ("scenario", "name", "level", "new_count", "columns", "rows")} for h in hits]}}[kind]
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        if r.status >= 300:
            raise RuntimeError(f"HTTP {r.status}")


def history(days=14):
    out, d = [], datetime.now(timezone.utc).date()
    for i in range(days):
        out += [{k: v for k, v in x.items() if k not in ("pk", "sk")} for x in query(f"ALERT#{(d - timedelta(days=i)).isoformat()}", forward=False)]
    return out
