"""用户与角色（Cognito 用户池：admin / user 两个组）+ 平台操作审计。"""
import re, secrets, string
from datetime import datetime, timedelta, timezone

from common import ApiError, USER_POOL_ID, client, now_iso, put, query

GROUPS = ("admin", "user")


def _cog():
    return client("cognito-idp")


def list_users():
    out, kw = [], {"UserPoolId": USER_POOL_ID, "Limit": 60}
    while True:
        r = _cog().list_users(**kw)
        for u in r["Users"]:
            attrs = {a["Name"]: a["Value"] for a in u.get("Attributes", [])}
            out.append({"username": u["Username"], "email": attrs.get("email"), "name": attrs.get("name", ""),
                        "status": u["UserStatus"], "enabled": u["Enabled"], "created": u["UserCreateDate"]})
        if not r.get("PaginationToken"):
            break
        kw["PaginationToken"] = r["PaginationToken"]
    admins = {u["Username"] for u in _cog().list_users_in_group(UserPoolId=USER_POOL_ID, GroupName="admin", Limit=60)["Users"]}
    for u in out:
        u["role"] = "admin" if u["username"] in admins else "user"
    return out


def temp_password():
    a = string.ascii_letters + string.digits
    return "La-" + "".join(secrets.choice(a) for _ in range(10)) + "9!"


def create_user(body):
    username = (body.get("username") or "").strip()
    email = (body.get("email") or "").strip()
    role = body.get("role") or "user"
    if not re.match(r"^[A-Za-z0-9._@-]{3,64}$", username) or role not in GROUPS:
        raise ApiError(400, "用户名 3-64 位字母数字 . _ @ -；角色 admin/user")
    pw = temp_password()
    attrs = [{"Name": "name", "Value": (body.get("name") or username)[:64]}]
    if email:
        attrs += [{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}]
    try:
        _cog().admin_create_user(UserPoolId=USER_POOL_ID, Username=username, TemporaryPassword=pw, UserAttributes=attrs,
                                 MessageAction="SUPPRESS")
    except _cog().exceptions.UsernameExistsException:
        raise ApiError(409, "用户名已存在")
    _cog().admin_add_user_to_group(UserPoolId=USER_POOL_ID, Username=username, GroupName=role)
    return {"username": username, "role": role, "temp_password": pw, "note": "首次登录需修改密码"}


def set_role(username, role, actor):
    if role not in GROUPS:
        raise ApiError(400, "角色非法")
    if username == actor and role != "admin":
        raise ApiError(400, "不能取消自己的管理员角色")
    for g in GROUPS:
        if g != role:
            _cog().admin_remove_user_from_group(UserPoolId=USER_POOL_ID, Username=username, GroupName=g)
    _cog().admin_add_user_to_group(UserPoolId=USER_POOL_ID, Username=username, GroupName=role)
    return {"ok": True}


def set_enabled(username, enabled, actor):
    if username == actor:
        raise ApiError(400, "不能停用自己")
    (_cog().admin_enable_user if enabled else _cog().admin_disable_user)(UserPoolId=USER_POOL_ID, Username=username)
    return {"ok": True}


def reset_password(username):
    pw = temp_password()
    _cog().admin_set_user_password(UserPoolId=USER_POOL_ID, Username=username, Password=pw, Permanent=False)
    return {"username": username, "temp_password": pw}


def delete_user(username, actor):
    if username == actor:
        raise ApiError(400, "不能删除自己")
    _cog().admin_delete_user(UserPoolId=USER_POOL_ID, Username=username)
    return {"ok": True}


# -------------------------------------------------------------- 操作审计
def record(actor, role, action, target, detail, status, ip, ms):
    ts = now_iso()
    put(f"AUDIT#{ts[:10]}", f"{ts}#{secrets.token_hex(3)}", ts=ts, actor=actor, role=role, action=action, target=target[:300],
        detail=detail[:2000], status=status, ip=ip, ms=ms)


def list_audit(day_from, day_to, actor=None, action=None):
    d1 = datetime.fromisoformat(day_from).date()
    d2 = datetime.fromisoformat(day_to).date()
    if (d2 - d1).days > 31:
        raise ApiError(400, "最多查询 31 天")
    out, d = [], d2
    while d >= d1:
        for it in query(f"AUDIT#{d.isoformat()}", forward=False):
            if actor and it.get("actor") != actor:
                continue
            if action and action not in it.get("action", ""):
                continue
            out.append({k: v for k, v in it.items() if k not in ("pk", "sk")})
            if len(out) >= 2000:
                return out
        d -= timedelta(days=1)
    return out
