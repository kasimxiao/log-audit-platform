#!/bin/bash
# 日志审计平台 - 主机侧采集脚本（由 SSM Run Command 以 root 执行，平台每次下发时渲染）
# 依赖：bash coreutils tar gzip curl（journalctl 可选；不依赖 last/lastlog 命令）
# 配置行格式：kind|binmode|logtype|path   kind=file|wtmp|btmp|lastlog|journal  binmode=text|raw|both
# 增量：按 inode+字节偏移；轮转后在同目录查找 inode 与上次相同的旧文件（.1/.0/-日期，未压缩）补采其尾部
# wtmp/btmp：按整条记录（x86_64 384 / aarch64 400 字节）增量取原始字节，由平台入库时解码（新发行版可能不再预装 last）
set -u
umask 077
CFG=$(echo '__CFG_B64__' | base64 -d)
URL=$(echo '__URL_B64__' | base64 -d)
SYNC_ID='__SYNC_ID__'
MAX_FILE_BYTES=$((512*1024*1024))
STATE_DIR=/var/lib/log-audit
STATE="$STATE_DIR/offsets"
LAST_OK="$STATE_DIR/last_success"
mkdir -p "$STATE_DIR"; touch "$STATE"
WORK=$(mktemp -d /tmp/log-audit.XXXXXX)
trap 'rm -rf "$WORK"' EXIT
OUT="$WORK/pkg"; mkdir -p "$OUT/files" "$OUT/text" "$OUT/raw" "$OUT/utmp"
NEWSTATE="$WORK/offsets.new"; cp "$STATE" "$NEWSTATE"
SINCE=""; [ -s "$LAST_OK" ] && SINCE=$(cat "$LAST_OK")
NOW=$(date -u '+%Y-%m-%d %H:%M:%S')
MAN="$WORK/manifest.jsonl"; : > "$MAN"
ARCH=$(uname -m); UTSZ=384   # struct utmp：x86_64 等为 384 字节；aarch64 为 400 字节（64 位 ut_session/ut_tv）
case "$ARCH" in aarch64|arm64) UTSZ=400;; esac
jesc() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }
man() { printf '{"kind":"%s","logtype":"%s","path":"%s","member":"%s","bytes":%s,"status":"%s","note":"%s"}\n' \
  "$1" "$2" "$(jesc "$3")" "$(jesc "$4")" "$5" "$6" "$(jesc "$7")" >> "$MAN"; }
setstate() { awk -v p="$1" '$1!=p' "$NEWSTATE" > "$NEWSTATE.t"; echo "$1 $2 $3" >> "$NEWSTATE.t"; mv "$NEWSTATE.t" "$NEWSTATE"; }
# 增量读取：$1 文件 $2 kind $3 logtype $4 包内根目录(files|utmp) $5 记录长度(1=按字节)
incr() {
  local f=$1 kind=$2 lt=$3 root=$4 rs=$5 ino size prev pino poff start=0 len note="" rot="" rlen=0 c csize
  ino=$(stat -c %i "$f"); size=$(stat -c %s "$f")
  prev=$(awk -v p="$f" '$1==p{print $2" "$3}' "$STATE" | tail -1)
  pino=${prev%% *}; poff=${prev##* }
  mkdir -p "$OUT/$root$(dirname "$f")"; : > "$OUT/$root$f"
  if [ -n "$prev" ]; then
    if [ "$pino" = "$ino" ] && [ "$size" -ge "$poff" ]; then start=$poff
    else
      for c in "$f".1 "$f".0 "$f"-[0-9]*; do
        case "$c" in *.gz|*.xz|*.bz2|*.zst) continue;; esac
        { [ -f "$c" ] && [ "$(stat -c %i "$c")" = "$pino" ]; } || continue
        csize=$(stat -c %s "$c")
        if [ "$csize" -gt "$poff" ]; then rlen=$((csize-poff)); [ "$rs" -gt 1 ] && rlen=$((rlen/rs*rs))
          tail -c +$((poff+1)) "$c" | head -c "$rlen" > "$OUT/$root$f"; fi
        rot="rotated:$c+${rlen}B"; break
      done
      [ -z "$rot" ] && rot="rotated_or_truncated:from_start"
    fi
  fi
  len=$((size-start)); [ "$rs" -gt 1 ] && len=$((len/rs*rs))
  if [ "$len" -gt "$MAX_FILE_BYTES" ]; then start=$((size-MAX_FILE_BYTES)); [ "$rs" -gt 1 ] && start=$(((start+rs-1)/rs*rs))
    len=$((size-start)); [ "$rs" -gt 1 ] && len=$((len/rs*rs)); note="truncated_to_last_512MB"; fi
  [ "$len" -gt 0 ] && tail -c +$((start+1)) "$f" | head -c "$len" >> "$OUT/$root$f"
  setstate "$f" "$ino" "$((start+len))"
  man "$kind" "$lt" "$f" "$root$f" "$((rlen+len))" ok "${rot}${note:+ $note}"
}
slug() { printf '%s' "$1" | tr '/:*' '__-'; }

shopt -s nullglob
while IFS='|' read -r KIND BINMODE LOGTYPE PATTERN; do
  [ -z "${KIND:-}" ] && continue
  case "$KIND" in
  file)
    matched=0
    for f in $PATTERN; do
      [ -f "$f" ] || continue; matched=1
      incr "$f" file "$LOGTYPE" files 1
    done
    [ $matched -eq 0 ] && man file "$LOGTYPE" "$PATTERN" "" 0 missing "no file matched"
    ;;
  wtmp|btmp)
    matched=0
    for f in $PATTERN; do
      [ -f "$f" ] || continue; matched=1
      if [ "$BINMODE" != "text" ]; then mkdir -p "$OUT/raw$(dirname "$f")"; cp "$f" "$OUT/raw$f"; man "$KIND" "$LOGTYPE" "$f" "raw$f" "$(stat -c %s "$f")" ok raw; fi
      [ "$BINMODE" != "raw" ] && incr "$f" "$KIND" "$LOGTYPE" utmp "$UTSZ"
    done
    [ $matched -eq 0 ] && man "$KIND" "$LOGTYPE" "$PATTERN" "" 0 missing "no file matched"
    ;;
  lastlog)
    matched=0
    for f in $PATTERN; do
      [ -f "$f" ] || continue; matched=1
      if [ "$BINMODE" != "text" ]; then mkdir -p "$OUT/raw$(dirname "$f")"; cp --sparse=always "$f" "$OUT/raw$f"; man lastlog "$LOGTYPE" "$f" "raw$f" "$(du -B1 "$OUT/raw$f" | cut -f1)" ok raw; fi
      if [ "$BINMODE" != "raw" ]; then
        # 直接按 UID 读取 struct lastlog（292 字节：int32 时间 + 32 字节终端 + 256 字节来源），不依赖 lastlog 命令
        t="$OUT/text/$(slug "$f").tsv"; : > "$t"; lsz=$(stat -c %s "$f")
        getent passwd | while IFS=: read -r name _ uid _; do
          off=$((uid*292)); tm=0; tty=""; host=""
          if [ $((off+292)) -le "$lsz" ]; then
            tm=$(od -An -t u4 -N4 -j "$off" "$f" | tr -d ' '); tm=${tm:-0}
            if [ "$tm" != "0" ]; then
              tty=$(dd if="$f" bs=1 skip=$((off+4)) count=32 2>/dev/null | tr -d '\000\t\n')
              host=$(dd if="$f" bs=1 skip=$((off+36)) count=256 2>/dev/null | tr -d '\000\t\n'); fi
          fi
          printf '%s\t%s\t%s\t%s\t%s\n' "$name" "$uid" "$tm" "$tty" "$host" >> "$t"
        done
        man lastlog "$LOGTYPE" "$f" "text/$(slug "$f").tsv" "$(stat -c %s "$t")" ok "decoded"
      fi
    done
    [ $matched -eq 0 ] && man lastlog "$LOGTYPE" "$PATTERN" "" 0 missing "no file matched"
    ;;
  journal)
    unit=${PATTERN#journal:}
    t="$OUT/text/$(slug "$PATTERN").txt"
    if command -v journalctl >/dev/null 2>&1; then
      s="${SINCE:-$(date -u -d '-7 days' '+%Y-%m-%d %H:%M:%S')}"
      if [ "$unit" = "*" ]; then journalctl --utc -o short-iso --no-pager --since "$s UTC" --until "$NOW UTC" > "$t" 2>/dev/null
      else journalctl --utc -o short-iso --no-pager -u "$unit" --since "$s UTC" --until "$NOW UTC" > "$t" 2>/dev/null; fi
      sed -i '/^-- /d' "$t"
      man journal "$LOGTYPE" "$PATTERN" "text/$(slug "$PATTERN").txt" "$(stat -c %s "$t")" ok "since $s"
    else man journal "$LOGTYPE" "$PATTERN" "" 0 error "journalctl not found"; fi
    ;;
  esac
done <<< "$CFG"

HN=$(hostname); OSID=$(. /etc/os-release 2>/dev/null; echo "${ID:-unknown}-${VERSION_ID:-}")
TZN=$(timedatectl show -p Timezone --value 2>/dev/null); [ -z "$TZN" ] && TZN=$(readlink /etc/localtime 2>/dev/null | sed 's|.*zoneinfo/||')
[ -z "$TZN" ] && TZN=$(cat /etc/timezone 2>/dev/null); [ -z "$TZN" ] && TZN=$(date +%Z); UTCOFF=$(date +%z)
{ printf '{"sync_id":"%s","host_name":"%s","os":"%s","arch":"%s","utmp_size":%s,"tz":"%s","utc_offset":"%s","collected_at":"%s","since":"%s","entries":[' "$SYNC_ID" "$(jesc "$HN")" "$OSID" "$ARCH" "$UTSZ" "$(jesc "$TZN")" "$UTCOFF" "$NOW" "$SINCE"
  paste -sd, "$MAN"; printf ']}\n'; } > "$OUT/manifest.json"
tar --sparse -C "$OUT" -czf "$WORK/pkg.tgz" . || { echo '{"status":"error","stage":"tar"}'; exit 2; }
BYTES=$(stat -c %s "$WORK/pkg.tgz")
HTTP=$(curl -sS -o "$WORK/resp" -w '%{http_code}' --retry 3 --max-time 900 -X PUT --upload-file "$WORK/pkg.tgz" "$URL")
if [ "$HTTP" = "200" ]; then
  mv "$NEWSTATE" "$STATE"; echo "$NOW" > "$LAST_OK"
  printf '{"status":"ok","sync_id":"%s","bytes":%s,"entries":%s}\n' "$SYNC_ID" "$BYTES" "$(wc -l < "$MAN")"
else
  echo "{\"status\":\"error\",\"stage\":\"upload\",\"http\":\"$HTTP\"}"; head -c 500 "$WORK/resp"; exit 3
fi
