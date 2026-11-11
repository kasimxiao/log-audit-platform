"""S3 staging/*.tgz → 解码/拆行/解析事件时间/预解析字段 → Parquet：
hostlogs/dt=同步日期/instance_id=实例/logtype=类型/批次.parquet，并登记 Glue 分区。
二进制原文件（raw）原样落 raw/。"""
import io, json, os, re, socket, struct, tarfile, tempfile, urllib.parse
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from common import GLUE_DB, client, jlog
import parsers

MON = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}
P_ISO = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:[.,]\d+)?(Z|[+-]\d{2}:?\d{2})?")
P_SYSLOG = re.compile(r"^([A-Z][a-z]{2})\s+(\d{1,2}) (\d{2}):(\d{2}):(\d{2})")
P_AUDIT = re.compile(r"msg=audit\((\d{9,11})\.\d+:\d+\)")
P_CLF = re.compile(r"\[(\d{2})/([A-Z][a-z]{2})/(\d{4}):(\d{2}):(\d{2}):(\d{2}) ([+-]\d{4})\]")
P_NGX_ERR = re.compile(r"^(\d{4})/(\d{2})/(\d{2}) (\d{2}:\d{2}:\d{2})")
P_HTTPD_ERR = re.compile(r"^\[[A-Z][a-z]{2} ([A-Z][a-z]{2}) (\d{2}) (\d{2}:\d{2}:\d{2})(?:\.\d+)? (\d{4})\]")
P_CTIME = re.compile(r"\b[A-Z][a-z]{2} ([A-Z][a-z]{2})\s+(\d{1,2}) (\d{2}:\d{2}:\d{2}) (\d{4})\b")
P_LAST = re.compile(r"\s(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})([+-]\d{2}:\d{2})?")

# glibc struct utmp：x86_64 等 384 字节（32 位 ut_session/ut_tv）；aarch64 400 字节（64 位 ut_session/ut_tv + 尾部对齐）
UTMP = {384: struct.Struct("<h2xi32s4s32s256s2hi2i4I20s"), 400: struct.Struct("<h2xi32s4s32s256s2hq2q4I20s4x")}
UT_TYPES = {0: "EMPTY", 1: "RUN_LVL", 2: "BOOT_TIME", 3: "NEW_TIME", 4: "OLD_TIME", 5: "INIT_PROCESS", 6: "LOGIN_PROCESS",
            7: "USER_PROCESS", 8: "DEAD_PROCESS", 9: "ACCOUNTING"}
TABLE = os.environ.get("HOSTLOGS_TABLE", "hostlogs")
BATCH = 20000


def _utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class TZ:
    """主机时区：用于不带时区信息的时间（syslog / nginx error / httpd error / ctime / 无偏移 ISO）。"""

    def __init__(self, name, offset):
        self.tz = timezone.utc
        try:
            if name and name not in ("UTC", "Etc/UTC", "Universal", "Zulu"):
                from zoneinfo import ZoneInfo
                self.tz = ZoneInfo(name)
                return
        except Exception:  # noqa
            pass
        m = re.match(r"^([+-])(\d{2})(\d{2})$", offset or "")
        if m and (m[2], m[3]) != ("00", "00"):
            sign = 1 if m[1] == "+" else -1
            self.tz = timezone(sign * timedelta(hours=int(m[2]), minutes=int(m[3])))

    def naive(self, y, mo, d, hms):
        h, mi, s = map(int, hms.split(":"))
        return _utc(datetime(y, mo, d, h, mi, s, tzinfo=self.tz))


def parse_time(line, ref_year, tz=None):
    """尽力解析一行日志的事件时间（UTC 'YYYY-MM-DD HH:MM:SS'）。"""
    tz = tz or TZ(None, None)
    try:
        m = P_ISO.match(line)
        if m:
            if m.group(3):
                z = m.group(3)
                z = "+00:00" if z == "Z" else (z if ":" in z else z[:3] + ":" + z[3:])
                return _utc(datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}{z}"))
            y, mo, d = map(int, m.group(1).split("-"))
            return tz.naive(y, mo, d, m.group(2))
        m = P_AUDIT.search(line)
        if m:
            return _utc(datetime.fromtimestamp(int(m.group(1)), timezone.utc))
        m = P_SYSLOG.match(line)
        if m:
            return tz.naive(ref_year, MON[m.group(1)], int(m.group(2)), f"{m.group(3)}:{m.group(4)}:{m.group(5)}")
        m = P_CLF.search(line)
        if m:
            return _utc(datetime.strptime(m.group(0)[1:-1], "%d/%b/%Y:%H:%M:%S %z"))
        m = P_NGX_ERR.match(line)
        if m:
            return tz.naive(int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4))
        m = P_HTTPD_ERR.match(line)
        if m:
            return tz.naive(int(m.group(4)), MON[m.group(1)], int(m.group(2)), m.group(3))
        m = P_CTIME.search(line)
        if m:
            return tz.naive(int(m.group(4)), MON[m.group(1)], int(m.group(2)), m.group(3))
        m = P_LAST.search(line)
        if m:
            if m.group(3):
                return _utc(datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}{m.group(3)}"))
            y, mo, d = map(int, m.group(1).split("-"))
            return tz.naive(y, mo, d, m.group(2))
    except Exception:  # noqa
        pass
    return None


# ------------------------------------------------------------------ 二进制登录记录解码
def _s(b):
    return b.split(b"\0", 1)[0].decode("utf-8", "replace").strip()


def decode_utmp(data, logtype, size=384):
    """yield (line, event_time, fields)。wtmp: 登录/注销/重启；btmp: 失败登录。size 为主机上报的记录长度。"""
    st = UTMP[size]
    for off in range(0, len(data) - len(data) % st.size, st.size):
        (t, pid, tty, _id, user, host, _e1, _e2, sess, sec, _usec, a0, a1, a2, a3, _u) = st.unpack_from(data, off)
        if t == 0 or sec <= 0:
            continue
        user, tty, host = _s(user), _s(tty), _s(host)
        ip = ""
        try:
            if a0 and not (a1 or a2 or a3):
                ip = socket.inet_ntop(socket.AF_INET, struct.pack("<I", a0))
            elif a0 or a1 or a2 or a3:
                ip = socket.inet_ntop(socket.AF_INET6, struct.pack("<4I", a0, a1, a2, a3))
        except Exception:  # noqa
            pass
        typ = UT_TYPES.get(t, str(t))
        when = datetime.fromtimestamp(sec, timezone.utc)
        if logtype == "login_btmp":
            action, result = "failed_login", "failed"
        else:
            action = {"USER_PROCESS": "login", "DEAD_PROCESS": "logout", "BOOT_TIME": "boot",
                      "RUN_LVL": "shutdown" if user == "shutdown" else "runlevel"}.get(typ, typ.lower())
            result = "success" if action == "login" else ""
        src = "" if typ in ("BOOT_TIME", "RUN_LVL") else (host or ip)  # 这两类的 host 字段是内核版本
        # 列顺序与 last 输出一致（用户 终端 来源 时间），便于沿用按列提取的写法
        line = f"{user or '-':<8} {tty or '-':<12} {src or '-':<16} {when.strftime('%a %b %d %H:%M:%S %Y')}  type={typ} pid={pid}"
        f = {"ut_type": typ, "action": action, "user": user, "tty": tty, "src_ip": src, "pid": str(pid), "session": str(sess),
             **({"kernel": host} if typ in ("BOOT_TIME", "RUN_LVL") and host else {})}
        if ip and ip != src:
            f["addr"] = ip
        if result:
            f["result"] = result
        yield line, _utc(when), {k: v for k, v in f.items() if v not in ("", None)}


def decode_lastlog_tsv(text):
    for row in text.splitlines():
        p = row.split("\t")
        if len(p) < 5:
            continue
        name, uid, tm, tty, host = p[:5]
        tm = int(tm or 0)
        when = _utc(datetime.fromtimestamp(tm, timezone.utc)) if tm > 0 else None
        line = f"{name:<16} uid={uid:<6} tty={tty or '-':<8} from={host or '-':<16} last_login={when or 'never'}"
        f = {"user": name, "uid": uid, "last_login": when or "never"}
        if tty:
            f["tty"] = tty
        if host:
            f["src_ip"] = host
        yield line, when, f


# ------------------------------------------------------------------ Parquet / Glue
@lru_cache(maxsize=1)
def _schema():
    import pyarrow as pa
    return pa.schema([("region", pa.string()), ("host_name", pa.string()), ("path", pa.string()), ("line_no", pa.int64()),
                      ("event_time", pa.string()), ("line", pa.string()), ("sync_id", pa.string()),
                      ("collected_at", pa.string()), ("fields", pa.map_(pa.string(), pa.string()))])


class Writers:
    """按 logtype 分别写 Parquet（流式分批），最后上传并登记分区。"""

    def __init__(self, td, db, bucket, prefix, dt, region, iid, sync_id):
        self.td, self.db, self.bucket, self.prefix = td, db, bucket, prefix
        self.dt, self.region, self.iid, self.sync_id = dt, region, iid, sync_id
        self.w, self.buf, self.count = {}, {}, {}

    def add(self, logtype, row):
        self.buf.setdefault(logtype, []).append(row)
        self.count[logtype] = self.count.get(logtype, 0) + 1
        if len(self.buf[logtype]) >= BATCH:
            self._flush(logtype)

    def _flush(self, lt):
        import pyarrow as pa, pyarrow.parquet as pq
        rows = self.buf.get(lt) or []
        if not rows:
            return
        cols = {k: [r[k] for r in rows] for k in _schema().names}
        cols["fields"] = [list(r["fields"].items()) for r in rows]
        tbl = pa.Table.from_pydict(cols, schema=_schema())
        if lt not in self.w:
            self.w[lt] = pq.ParquetWriter(os.path.join(self.td, f"{lt}.parquet"), _schema(), compression="snappy")
        self.w[lt].write_table(tbl)
        self.buf[lt] = []

    def close(self):
        keys = []
        for lt in list(self.buf):
            self._flush(lt)
        for lt, w in self.w.items():
            w.close()
            loc = f"{self.prefix}dt={self.dt}/instance_id={self.iid}/logtype={lt}/"
            client("s3").upload_file(os.path.join(self.td, f"{lt}.parquet"), self.bucket, f"{loc}{self.region}_{self.sync_id}.parquet")
            keys.append((lt, f"s3://{self.bucket}/{loc}"))
        register_partitions(self.db, [([self.dt, self.iid, lt], loc) for lt, loc in keys])
        return self.count


@lru_cache(maxsize=4)
def _table_sd(db):
    return client("glue").get_table(DatabaseName=db, Name=TABLE)["Table"]["StorageDescriptor"]


def register_partitions(db, parts):
    """parts: [([dt, instance_id, logtype], location)]；已存在的忽略。"""
    if not parts:
        return
    sd = _table_sd(db)
    base = {k: sd[k] for k in ("Columns", "InputFormat", "OutputFormat", "SerdeInfo") if k in sd}
    for i in range(0, len(parts), 100):
        r = client("glue").batch_create_partition(DatabaseName=db, TableName=TABLE, PartitionInputList=[
            {"Values": v, "StorageDescriptor": {**base, "Location": loc}} for v, loc in parts[i:i + 100]])
        bad = [e for e in r.get("Errors", []) if e["ErrorDetail"]["ErrorCode"] != "AlreadyExistsException"]
        if bad:
            raise RuntimeError(f"登记分区失败: {bad[:2]}")


# ------------------------------------------------------------------ 主流程
def handler(event, _ctx):
    for rec in event.get("Records", []):
        key = urllib.parse.unquote_plus(rec["s3"]["object"]["key"])
        if key.startswith("staging/") and key.endswith(".tgz"):
            process(rec["s3"]["bucket"]["name"], key)


def _display_name(sync_id):
    from common import get
    idx = get("SYNCIDX", sync_id)
    if idx:
        rec = get(idx["pk2"], idx["sk2"])
        return (rec or {}).get("name")
    return None


def rows_from_member(tf, member, entry, ref_year, tz, utmp_size=384):
    """把包内一个成员转成 (line, event_time, fields) 序列。"""
    fobj = tf.extractfile(member)
    if entry["member"].startswith("utmp/"):
        yield from decode_utmp(fobj.read(), entry["logtype"], utmp_size)
        return
    if entry["member"].endswith(".tsv") and entry["kind"] == "lastlog":
        yield from decode_lastlog_tsv(fobj.read().decode("utf-8", "replace"))
        return
    last_t = None
    for line in io.TextIOWrapper(fobj, encoding="utf-8", errors="replace"):
        line = line.rstrip("\n")
        if not line.strip():
            continue
        line = line[:16000]
        t = parse_time(line, ref_year, tz) or last_t  # 多行日志续行沿用上一行时间
        last_t = t
        yield line, t, parsers.parse_fields(entry["logtype"], line)


def process(bucket, key):
    from syncsvc import mark_ingested
    _, region, iid, fname = key.split("/", 3)
    sync_id = fname[:-4]
    s3 = client("s3")
    with tempfile.TemporaryDirectory() as td:
        tgz = os.path.join(td, "pkg.tgz")
        s3.download_file(bucket, key, tgz)
        stats = ingest_package(tgz, bucket, region, iid, sync_id, GLUE_DB, "hostlogs/", td, _display_name(sync_id))
    mark_ingested(sync_id, stats)
    s3.delete_object(Bucket=bucket, Key=key)
    jlog(event="ingested", sync_id=sync_id, lines=stats["lines"])
    return stats


def ingest_package(tgz, bucket, region, iid, sync_id, db, prefix, td, display_name=None):
    with tarfile.open(tgz, "r:gz") as tf:
        members = {m.name.lstrip("./"): m for m in tf.getmembers() if m.isfile()}
        manifest = json.load(tf.extractfile(members["manifest.json"]))
        coll = manifest.get("collected_at") or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        dt, ref_year = coll[:10], int(coll[:4])
        tz = TZ(manifest.get("tz"), manifest.get("utc_offset"))
        host = display_name or manifest.get("host_name", "")
        W = Writers(td, db, bucket, prefix, dt, region, iid, sync_id)
        per = {}
        for e in manifest.get("entries", []):
            mem = e.get("member")
            if not mem or mem not in members:
                continue
            if mem.startswith("raw/"):
                s3 = client("s3")
                s3.upload_fileobj(tf.extractfile(members[mem]), bucket, f"raw/dt={dt}/{region}/{iid}/{sync_id}{mem[3:]}")
                continue
            n = 0
            for line, t, f in rows_from_member(tf, members[mem], e, ref_year, tz, int(manifest.get("utmp_size") or 384)):
                n += 1
                W.add(e["logtype"], {"region": region, "host_name": host, "path": e["path"], "line_no": n, "event_time": t,
                                     "line": line, "sync_id": sync_id, "collected_at": coll, "fields": f})
            per[e["path"]] = per.get(e["path"], 0) + n
        counts = W.close()
        client("s3").put_object(Bucket=bucket, Key=f"manifests/dt={dt}/{region}_{iid}_{sync_id}.json",
                                Body=json.dumps(manifest, ensure_ascii=False).encode())
    return {"lines": sum(counts.values()), "per_path": per, "per_logtype": counts, "dt": dt, "os": manifest.get("os"),
            "tz": manifest.get("tz"), "missing": [e["path"] for e in manifest.get("entries", []) if e.get("status") != "ok"],
            "notes": [f"{e['path']}: {e['note']}" for e in manifest.get("entries", []) if e.get("note", "").startswith("rotated")]}
