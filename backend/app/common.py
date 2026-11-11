"""公共：环境变量、AWS 客户端缓存、DynamoDB 单表、响应封装。"""
import base64, decimal, json, os, re, time, uuid
from datetime import datetime, timezone
from functools import lru_cache

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config

TABLE = os.environ.get("TABLE_NAME", "LogAudit")
LOG_BUCKET = os.environ.get("LOG_BUCKET", "")
HOME_REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
GLUE_DB = os.environ.get("GLUE_DB", "logaudit")
EVAL_DB = os.environ.get("EVAL_DB", "logaudit_eval")
WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "logaudit")
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
INSTANCE_PROFILE_ARN = os.environ.get("INSTANCE_PROFILE_ARN", "")
WORKER_FN = os.environ.get("WORKER_FN", "")
ACCOUNT_ID = os.environ.get("ACCOUNT_ID", "")

_cfg = Config(retries={"max_attempts": 5, "mode": "adaptive"}, connect_timeout=5, read_timeout=60)


@lru_cache(maxsize=64)
def client(service, region=None):
    return boto3.client(service, region_name=region or HOME_REGION, config=_cfg)


@lru_cache(maxsize=1)
def table():
    return boto3.resource("dynamodb", region_name=HOME_REGION).Table(TABLE)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix=""):
    return prefix + uuid.uuid4().hex[:12]


class ApiError(Exception):
    def __init__(self, status, msg, code="error"):
        super().__init__(msg)
        self.status, self.msg, self.code = status, msg, code


def _default(o):
    if isinstance(o, decimal.Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, (set, frozenset)):
        return list(o)
    if isinstance(o, datetime):
        return o.isoformat()
    return str(o)


def dumps(o):
    return json.dumps(o, default=_default, ensure_ascii=False)


def resp(status, body):
    return {"statusCode": status, "headers": {"content-type": "application/json; charset=utf-8", "cache-control": "no-store"},
            "body": dumps(body)}


def to_ddb(o):
    """float → Decimal 以便写 DynamoDB。"""
    return json.loads(json.dumps(o, default=_default), parse_float=decimal.Decimal)


# ------------------------------------------------------------ DynamoDB 单表
def put(pk, sk, **attrs):
    item = {"pk": pk, "sk": sk, **attrs}
    table().put_item(Item=to_ddb(item))
    return item


def get(pk, sk):
    return table().get_item(Key={"pk": pk, "sk": sk}).get("Item")


def delete(pk, sk):
    table().delete_item(Key={"pk": pk, "sk": sk})


def query(pk, sk_prefix=None, limit=None, forward=True):
    kw = {"KeyConditionExpression": Key("pk").eq(pk), "ScanIndexForward": forward}
    if sk_prefix:
        kw["KeyConditionExpression"] &= Key("sk").begins_with(sk_prefix)
    out = []
    while True:
        if limit:
            kw["Limit"] = limit - len(out)
        r = table().query(**kw)
        out += r["Items"]
        if "LastEvaluatedKey" not in r or (limit and len(out) >= limit):
            return out
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


# ------------------------------------------------------------ 校验
RE_REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
RE_IID = re.compile(r"^i-[0-9a-f]{8,17}$")
RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RE_SAFE_PATH = re.compile(r"^/[A-Za-z0-9._/@+*?\[\]-]+$")
RE_JOURNAL = re.compile(r"^journal:[A-Za-z0-9@._*-]+$")


def v_region(r):
    if not r or not RE_REGION.match(r):
        raise ApiError(400, f"非法区域: {r}")
    return r


def v_iid(i):
    if not i or not RE_IID.match(i):
        raise ApiError(400, f"非法实例 ID: {i}")
    return i


def v_date(d):
    if not d or not RE_DATE.match(d):
        raise ApiError(400, f"日期格式应为 YYYY-MM-DD: {d}")
    return d


def v_log_path(p, allow_glob=True):
    """采集/查询路径白名单字符集，禁止 .. 与空白、引号、$ 等 shell 元字符。"""
    if not isinstance(p, str) or len(p) > 256:
        raise ApiError(400, "路径非法")
    if RE_JOURNAL.match(p):
        return p
    if ".." in p or not RE_SAFE_PATH.match(p) or (not allow_glob and any(c in p for c in "*?[]")):
        raise ApiError(400, f"路径非法（仅允许字母数字 . _ - / 和通配符）: {p}")
    return p


def sql_str(s):
    """Athena 字符串字面量转义。"""
    if s is None:
        return "NULL"
    s = str(s)
    if "\x00" in s:
        raise ApiError(400, "非法字符")
    return "'" + s.replace("'", "''") + "'"


def b64(s):
    return base64.b64encode(s.encode()).decode()


def jlog(**kw):
    print(dumps({"ts": time.time(), **kw}))
