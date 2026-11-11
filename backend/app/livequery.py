"""实时查询：经 SSM 在指定主机上 列目录 / tail / grep / 二进制转文本。
安全：路径与正则 base64 传入，脚本内仅作为引号变量使用（不 eval）；路径必须位于允许根目录下。
输出 gzip+base64 回传，绕开 SSM 24KB 标准输出截断，主机无需 S3 权限。"""
import base64, gzip, json

from common import ApiError, b64, client, get, now_iso, put, v_iid, v_log_path, v_region

DEFAULT_ROOTS = ["/var/log/"]

SCRIPT = r'''#!/bin/bash
set -u
MODE='__MODE__'; P=$(echo '__P__' | base64 -d); PAT=$(echo '__PAT__' | base64 -d); N=__N__; IC='__IC__'; CTX=__CTX__
LIMIT=$((6*1024*1024)); T=$(mktemp); trap 'rm -f "$T"' EXIT
case "$MODE" in
  ls)   if [ -d "$P" ]; then find "$P" -maxdepth 2 \( -type f -o -type d \) -printf '%y\t%s\t%TY-%Tm-%Td %TH:%TM\t%p\n' 2>/dev/null | sort -k4 | head -2000 > "$T"
        else echo "__ERR__ not a directory" > "$T"; fi ;;
  tail) if [ -f "$P" ]; then tail -n "$N" "$P" > "$T"; else echo "__ERR__ file not found" > "$T"; fi ;;
  grep) if [ -f "$P" ]; then
          case "$P" in *.gz) R=zgrep;; *) R=grep;; esac
          $R -n -a -E $IC -C "$CTX" -m "$N" -e "$PAT" -- "$P" > "$T" 2>&1; rc=$?; [ $rc -eq 1 ] && echo "__NOMATCH__" > "$T"
        else echo "__ERR__ file not found" > "$T"; fi ;;
  last) if [ ! -f "$P" ]; then echo "__ERR__ file not found" > "$T"
        elif command -v last >/dev/null 2>&1; then last -F -w -i -f "$P" 2>&1 | head -n "$N" > "$T"
        elif command -v python3 >/dev/null 2>&1; then python3 - "$P" "$N" > "$T" 2>&1 <<'PY'
import struct, sys, time, socket
p, n = sys.argv[1], int(sys.argv[2]); import platform
fmt = "<h2xi32s4s32s256s2hq2q4I20s4x" if platform.machine() in ("aarch64", "arm64") else "<h2xi32s4s32s256s2hi2i4I20s"
st = struct.Struct(fmt); d = open(p, "rb").read(); T = {1: "RUN_LVL", 2: "BOOT", 5: "INIT", 6: "LOGIN", 7: "USER", 8: "DEAD"}
rows = []
for o in range(0, len(d) - len(d) % st.size, st.size):
    t, pid, line, _i, user, host, _a, _b, _s, sec = st.unpack_from(d, o)[:10]
    if t == 0 or sec <= 0: continue
    z = lambda b: b.split(b"\0", 1)[0].decode("utf-8", "replace")
    rows.append(f"{z(user) or '-':<8} {z(line) or '-':<12} {z(host) or '-':<16} {time.strftime('%a %b %d %H:%M:%S %Y', time.gmtime(sec))} UTC  {T.get(t, t)} pid={pid}")
print("\n".join(reversed(rows[-n:])) or "(empty)")
PY
        else echo "__ERR__ 主机既没有 last 也没有 python3" > "$T"; fi ;;
  lastlog) LL=/var/log/lastlog; : > "$T"
        if command -v lastlog >/dev/null 2>&1; then lastlog | head -n "$N" > "$T"
        elif [ -f "$LL" ]; then lsz=$(stat -c %s "$LL")
          getent passwd | head -n "$N" | while IFS=: read -r name _ uid _; do off=$((uid*292)); tm=0; tty=""; host=""
            if [ $((off+292)) -le "$lsz" ]; then tm=$(od -An -t u4 -N4 -j "$off" "$LL" | tr -d ' '); tm=${tm:-0}
              [ "$tm" != "0" ] && tty=$(dd if="$LL" bs=1 skip=$((off+4)) count=32 2>/dev/null | tr -d '\000') && host=$(dd if="$LL" bs=1 skip=$((off+36)) count=256 2>/dev/null | tr -d '\000'); fi
            if [ "$tm" = "0" ]; then printf '%-16s **Never logged in**\n' "$name" >> "$T"; else printf '%-16s %-8s %-16s %s\n' "$name" "$tty" "$host" "$(date -u -d @"$tm" '+%Y-%m-%d %H:%M:%S UTC')" >> "$T"; fi
          done
        else echo "__ERR__ /var/log/lastlog not found" > "$T"; fi ;;
  journal) journalctl --utc -o short-iso --no-pager -u "$P" -n "$N" ${PAT:+--grep "$PAT"} > "$T" 2>&1 ;;
esac
SZ=$(stat -c %s "$T"); TR=0
if [ "$SZ" -gt "$LIMIT" ]; then head -c "$LIMIT" "$T" > "$T.c"; mv "$T.c" "$T"; TR=1; fi
# 压缩后 base64 需 < 23000 字符：超出则按行数折半
while :; do B=$(gzip -c "$T" | base64 -w0); [ ${#B} -lt 23000 ] && break
  L=$(wc -l < "$T"); [ "$L" -le 1 ] && { B=$(echo "__ERR__ output too large" | gzip -c | base64 -w0); break; }
  if [ "$MODE" = "tail" ]; then tail -n $((L/2)) "$T" > "$T.c"; else head -n $((L/2)) "$T" > "$T.c"; fi; mv "$T.c" "$T"; TR=1; done
echo "LA1 $TR $SZ $B"
'''


def allowed_roots():
    s = get("SETTINGS", "live_roots")
    return (s or {}).get("roots") or DEFAULT_ROOTS


def set_roots(roots, actor):
    clean = []
    for r in roots:
        r = v_log_path(r.strip(), allow_glob=False)
        if not r.endswith("/"):
            r += "/"
        if r in ("/", "/etc/", "/root/", "/proc/") or len(r) < 3:
            raise ApiError(400, f"不允许的根目录: {r}")
        clean.append(r)
    put("SETTINGS", "live_roots", roots=clean, updated_by=actor, updated_at=now_iso())
    return {"roots": clean}


def display_cmd(mode, path, pat, n, ic, ctx):
    """主机上实际执行的等价命令（展示用；真实脚本中路径/正则经 base64 传入并作为引号变量使用）。"""
    import shlex
    q = shlex.quote
    if mode == "ls":
        return f"find {q(path)} -maxdepth 2 \\( -type f -o -type d \\) -printf '%y\\t%s\\t%TY-%Tm-%Td %TH:%TM\\t%p\\n' | sort -k4 | head -2000"
    if mode == "tail":
        return f"tail -n {n} {q(path)}"
    if mode == "grep":
        g = "zgrep" if path.endswith(".gz") else "grep"
        return f"{g} -n -a -E{' -i' if ic else ''} -C {ctx} -m {n} -e {q(pat)} -- {q(path)}"
    if mode == "last":
        return f"last -F -w -i -f {q(path)} | head -n {n}    # 无 last 命令时用 python3 按 struct utmp 解码"
    if mode == "lastlog":
        return f"lastlog | head -n {n}    # 无 lastlog 命令时按 UID 直接读取 /var/log/lastlog"
    return f"journalctl --utc -o short-iso --no-pager -u {q(path)} -n {n}" + (f" --grep {q(pat)}" if pat else "")


def start(body, actor):
    region, iid = v_region(body.get("region")), v_iid(body.get("instance_id"))
    mode = body.get("mode", "tail")
    if mode not in ("ls", "tail", "grep", "last", "lastlog", "journal"):
        raise ApiError(400, "mode 非法")
    path = (body.get("path") or "").strip()
    if mode == "journal":
        unit = path or "sshd"
        if not all(c.isalnum() or c in "@._-" for c in unit):
            raise ApiError(400, "unit 非法")
        path = unit
    elif mode != "lastlog":
        v_log_path(path, allow_glob=False)
        if not any(path == r.rstrip("/") or path.startswith(r) for r in allowed_roots()):
            raise ApiError(403, f"路径不在允许的根目录内: {allowed_roots()}")
    pat = body.get("pattern") or ""
    if mode == "grep" and not pat:
        raise ApiError(400, "grep 需要 pattern")
    if len(pat) > 500:
        raise ApiError(400, "pattern 过长")
    n = max(1, min(int(body.get("lines") or 200), 5000))
    ctx = max(0, min(int(body.get("context") or 0), 5))
    script = (SCRIPT.replace("__MODE__", mode).replace("__P__", b64(path)).replace("__PAT__", b64(pat))
              .replace("__N__", str(n)).replace("__IC__", "-i" if body.get("ignore_case") else "").replace("__CTX__", str(ctx)))
    cmd = client("ssm", region).send_command(InstanceIds=[iid], DocumentName="AWS-RunShellScript", TimeoutSeconds=60,
                                             Comment=f"log-audit live {mode} by {actor}"[:100],
                                             Parameters={"commands": [script], "executionTimeout": ["120"]})
    cid = cmd["Command"]["CommandId"]
    shown = display_cmd(mode, path, pat, n, bool(body.get("ignore_case")), ctx)
    put("LIVE", cid, owner=actor, region=region, instance_id=iid, mode=mode, path=path, pattern=pat, created_at=now_iso(),
        command=shown, script=script)
    return {"command_id": cid, "command": shown, "document": "AWS-RunShellScript", "region": region, "instance_id": iid}


def result(cid, actor, is_admin):
    rec = get("LIVE", cid)
    if not rec or (rec["owner"] != actor and not is_admin):
        raise ApiError(404, "查询不存在")
    try:
        inv = client("ssm", rec["region"]).get_command_invocation(CommandId=cid, InstanceId=rec["instance_id"])
    except client("ssm", rec["region"]).exceptions.InvocationDoesNotExist:
        return {"status": "Pending"}
    st = inv["Status"]
    if st in ("Pending", "InProgress", "Delayed"):
        return {"status": st, "command": rec.get("command")}
    outp = inv.get("StandardOutputContent") or ""
    line = next((l for l in outp.splitlines() if l.startswith("LA1 ")), None)
    if not line:
        return {"status": st, "error": (inv.get("StandardErrorContent") or outp)[-1000:], "command": rec.get("command"),
                "script": rec.get("script"), "command_id": cid}
    _, tr, sz, b = line.split(" ", 3)
    text = gzip.decompress(base64.b64decode(b)).decode("utf-8", "replace")
    out = {"status": st, "truncated": tr == "1", "bytes": int(sz), "mode": rec["mode"], "path": rec["path"],
           "command": rec.get("command"), "script": rec.get("script"), "command_id": cid}
    if text.startswith("__ERR__"):
        out["error"] = text[8:].strip()
    elif text.startswith("__NOMATCH__"):
        out["text"], out["nomatch"] = "", True
    elif rec["mode"] == "ls":
        rows = []
        for l in text.splitlines():
            p = l.split("\t")
            if len(p) == 4:
                rows.append({"type": "dir" if p[0] == "d" else "file", "size": int(p[1]), "mtime": p[2], "path": p[3]})
        out["entries"] = rows
    else:
        out["text"] = text
    return out
