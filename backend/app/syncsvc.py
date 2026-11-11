"""采集模板 + 每日同步下发 + 同步记录。"""
import os
from datetime import datetime, timezone

from common import (ApiError, LOG_BUCKET, b64, client, delete, get, jlog, new_id, now_iso, put, query, table, v_iid,
                    v_region)
from hosts import _norm_paths, all_enabled_cfgs, get_cfg

DEFAULT_TEMPLATES = [
    {"id": "linux-security", "name": "Linux 安全基线（RHEL/CentOS/AL2 rsyslog）", "paths": [
        {"path": "/var/log/secure", "logtype": "auth"},
        {"path": "/var/log/audit/audit.log", "logtype": "audit"},
        {"path": "/var/log/wtmp", "logtype": "login_wtmp", "binmode": "text"},
        {"path": "/var/log/btmp", "logtype": "login_btmp", "binmode": "text"},
        {"path": "/var/log/lastlog", "logtype": "lastlog", "binmode": "text"},
        {"path": "/var/log/cron", "logtype": "cron"}]},
    {"id": "ubuntu-security", "name": "Ubuntu 安全基线", "paths": [
        {"path": "/var/log/auth.log", "logtype": "auth"},
        {"path": "/var/log/audit/audit.log", "logtype": "audit"},
        {"path": "/var/log/wtmp", "logtype": "login_wtmp", "binmode": "text"},
        {"path": "/var/log/btmp", "logtype": "login_btmp", "binmode": "text"},
        {"path": "/var/log/lastlog", "logtype": "lastlog", "binmode": "text"},
        {"path": "/var/log/syslog", "logtype": "syslog"}]},
    {"id": "al2023-journal", "name": "Amazon Linux 2023（默认无 rsyslog，走 journald）", "paths": [
        {"path": "journal:sshd", "logtype": "auth"},
        {"path": "journal:crond", "logtype": "cron"},
        {"path": "/var/log/audit/audit.log", "logtype": "audit"},
        {"path": "/var/log/wtmp", "logtype": "login_wtmp", "binmode": "text"},
        {"path": "/var/log/btmp", "logtype": "login_btmp", "binmode": "text"},
        {"path": "/var/log/lastlog", "logtype": "lastlog", "binmode": "text"}]},
    {"id": "web-nginx", "name": "Nginx", "paths": [
        {"path": "/var/log/nginx/access.log", "logtype": "nginx_access"},
        {"path": "/var/log/nginx/error.log", "logtype": "nginx_error"}]},
    {"id": "web-httpd", "name": "Apache httpd（待定）", "paths": [
        {"path": "/var/log/httpd/access_log", "logtype": "httpd_access"},
        {"path": "/var/log/httpd/error_log", "logtype": "httpd_error"},
        {"path": "/var/log/apache2/access.log", "logtype": "httpd_access"},
        {"path": "/var/log/apache2/error.log", "logtype": "httpd_error"}]},
]


def list_templates():
    items = query("TEMPLATE")
    if not items:
        for t in DEFAULT_TEMPLATES:
            put("TEMPLATE", t["id"], id=t["id"], name=t["name"], paths=_norm_paths(t["paths"]), builtin=True,
                updated_at=now_iso(), updated_by="system")
        items = query("TEMPLATE")
    return [{k: v for k, v in i.items() if k not in ("pk", "sk")} for i in items]


def save_template(body, actor):
    tid = (body.get("id") or new_id("tpl-")).strip()
    if not tid.replace("-", "").replace("_", "").isalnum() or len(tid) > 48:
        raise ApiError(400, "模板 ID 仅允许字母数字 - _")
    name = (body.get("name") or tid)[:80]
    paths = _norm_paths(body.get("paths"))
    if not paths:
        raise ApiError(400, "至少一条路径")
    put("TEMPLATE", tid, id=tid, name=name, paths=paths, builtin=False, updated_at=now_iso(), updated_by=actor)
    return {"id": tid}


def delete_template(tid):
    delete("TEMPLATE", tid)
    return {"ok": True}


def effective_paths(cfg):
    paths, seen = [], set()
    for tid in cfg.get("templates", []):
        t = get("TEMPLATE", tid)
        for p in (t or {}).get("paths", []):
            if p["path"] not in seen:
                seen.add(p["path"]); paths.append(p)
    for p in cfg.get("extra_paths", []):
        if p["path"] not in seen:
            seen.add(p["path"]); paths.append(p)
    return paths


def _presigner():
    """区域端点 + SigV4：避免全局端点 307 重定向导致主机 PUT 失败。"""
    import boto3
    from botocore.config import Config
    from common import HOME_REGION
    return boto3.client("s3", region_name=HOME_REGION, endpoint_url=f"https://s3.{HOME_REGION}.amazonaws.com",
                        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))


_SCRIPT = open(os.path.join(os.path.dirname(__file__), "collector.sh")).read()


def render_script(paths, url, sync_id):
    lines = "\n".join(f'{p["kind"]}|{p.get("binmode", "text")}|{p["logtype"]}|{p["path"]}' for p in paths)
    return _SCRIPT.replace("__CFG_B64__", b64(lines)).replace("__URL_B64__", b64(url)).replace("__SYNC_ID__", sync_id)


def dispatch(targets=None, actor="scheduler", trigger="schedule"):
    """targets: [(region, iid)]；None = 所有开启同步的主机。"""
    cfgs = all_enabled_cfgs() if targets is None else [
        dict(get_cfg(v_region(r), v_iid(i)), region=r, instance_id=i) for r, i in targets]
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    results = []
    for c in cfgs:
        region, iid = c["region"], c["instance_id"]
        sync_id = new_id(f"{day.replace('-', '')}-")
        rec = {"region": region, "instance_id": iid, "name": c.get("name", ""), "sync_id": sync_id, "trigger": trigger,
               "actor": actor, "started_at": now_iso(), "day": day}
        paths = effective_paths(c)
        if not paths:
            rec.update(status="skipped", message="无采集路径")
        else:
            key = f"staging/{region}/{iid}/{sync_id}.tgz"
            url = _presigner().generate_presigned_url("put_object", Params={"Bucket": LOG_BUCKET, "Key": key}, ExpiresIn=3600)
            try:
                script = render_script(paths, url, sync_id)
                cmd = client("ssm", region).send_command(
                    InstanceIds=[iid], DocumentName="AWS-RunShellScript", TimeoutSeconds=600,
                    Comment=f"log-audit sync {sync_id}"[:100],
                    Parameters={"commands": [script], "executionTimeout": ["1800"]})
                rec.update(status="dispatched", command_id=cmd["Command"]["CommandId"], paths=len(paths),
                           # 展示用：预签名 URL 脱敏，配置行明文
                           script=render_script(paths, "https://<预签名 PUT URL，1 小时有效，已隐藏>", sync_id),
                           config_lines=[f'{p["kind"]}|{p.get("binmode", "text")}|{p["logtype"]}|{p["path"]}' for p in paths])
            except Exception as e:  # noqa
                rec.update(status="dispatch_failed", message=str(e)[:300])
        put(f"SYNC#{day}", f"{region}#{iid}#{sync_id}", **rec)
        put("SYNCIDX", sync_id, pk2=f"SYNC#{day}", sk2=f"{region}#{iid}#{sync_id}")
        results.append(rec)
    jlog(event="sync_dispatch", count=len(results), trigger=trigger)
    return results


def history(day, refresh=True):
    items = query(f"SYNC#{day}")
    for it in items:
        need = it.get("command_id") and not it.get("output_fetched") and it.get("status") in ("dispatched", "ingested")
        if not (refresh and need):
            continue
        try:
            inv = client("ssm", it["region"]).get_command_invocation(CommandId=it["command_id"], InstanceId=it["instance_id"])
            st = inv["Status"]
            if st not in ("Success", "Failed", "TimedOut", "Cancelled"):
                continue
            out, err = (inv.get("StandardOutputContent") or "")[-600:], (inv.get("StandardErrorContent") or "")[-600:]
            it.update(host_output=out, host_error=err, output_fetched=True)
            vals = {":o": out, ":e": err, ":t": True}
            expr = "SET host_output=:o, host_error=:e, output_fetched=:t"
            if it["status"] == "dispatched":  # 未入库：按主机结果更新状态
                new = "uploaded" if st == "Success" else "host_failed"
                it["status"] = new
                expr += ", #s=:s, finished_at=:f"; vals.update({":s": new, ":f": now_iso()})
                # 条件：仍是 dispatched 才改状态，防止覆盖 extractor 写入的 ingested
                table().update_item(Key={"pk": it["pk"], "sk": it["sk"]}, UpdateExpression=expr, ConditionExpression="#s = :d",
                                    ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={**vals, ":d": "dispatched"})
            else:
                table().update_item(Key={"pk": it["pk"], "sk": it["sk"]}, UpdateExpression=expr, ExpressionAttributeValues=vals)
        except Exception as e:  # noqa
            it["refresh_error"] = str(e)[:200]
    return [{k: v for k, v in i.items() if k not in ("pk", "sk")} for i in sorted(items, key=lambda x: x.get("started_at", ""), reverse=True)]


def mark_ingested(sync_id, stats):
    idx = get("SYNCIDX", sync_id)
    if not idx:
        return
    table().update_item(Key={"pk": idx["pk2"], "sk": idx["sk2"]},
                        UpdateExpression="SET #s=:s, ingest=:i, ingested_at=:t", ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":s": "ingested", ":i": stats, ":t": now_iso()})
    parts = idx["sk2"].split("#")
    try:
        table().update_item(Key={"pk": f"HOSTCFG#{parts[0]}", "sk": parts[1]}, UpdateExpression="SET last_sync=:l",
                            ConditionExpression="attribute_exists(pk)",
                            ExpressionAttributeValues={":l": {"sync_id": sync_id, "at": now_iso(), "lines": stats.get("lines", 0)}})
    except Exception:  # noqa
        pass


def scheduled(event, _ctx):
    """EventBridge Scheduler 每日触发。"""
    res = dispatch(None, actor="scheduler", trigger="schedule")
    return {"dispatched": sum(1 for r in res if r.get("status") == "dispatched"), "total": len(res)}
