"""主机：按区域罗列 EC2、SSM 纳管状态、采集配置、一键挂实例角色。"""
from concurrent.futures import ThreadPoolExecutor

from common import (ApiError, INSTANCE_PROFILE_ARN, client, delete, get, now_iso, put, query, v_iid, v_log_path,
                    v_region)

DEFAULT_REGIONS_KEY = ("SETTINGS", "regions")


def enabled_regions():
    r = client("ec2").describe_regions(Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}])
    return sorted(x["RegionName"] for x in r["Regions"])


def region_summary():
    """每个区域的实例数（并发扫描）。"""
    regs = enabled_regions()

    def cnt(r):
        try:
            n = 0
            for page in client("ec2", r).get_paginator("describe_instances").paginate(
                    Filters=[{"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}]):
                n += sum(len(x["Instances"]) for x in page["Reservations"])
            return {"region": r, "instances": n}
        except Exception as e:  # noqa
            return {"region": r, "instances": None, "error": str(e)[:200]}

    with ThreadPoolExecutor(16) as ex:
        return list(ex.map(cnt, regs))


def _ssm_map(region):
    m = {}
    for page in client("ssm", region).get_paginator("describe_instance_information").paginate():
        for i in page["InstanceInformationList"]:
            m[i["InstanceId"]] = {"ping": i.get("PingStatus"), "agent": i.get("AgentVersion"),
                                  "platform": f'{i.get("PlatformName", "")} {i.get("PlatformVersion", "")}'.strip(),
                                  "last_ping": i.get("LastPingDateTime")}
    return m


def list_hosts(region, tag_key=None, tag_value=None):
    v_region(region)
    flt = [{"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}]
    if tag_key:
        flt.append({"Name": f"tag:{tag_key}", "Values": [tag_value or "*"]})
    insts = []
    for page in client("ec2", region).get_paginator("describe_instances").paginate(Filters=flt):
        for res in page["Reservations"]:
            insts += res["Instances"]
    ssm = _ssm_map(region)
    cfgs = {c["sk"]: c for c in query(f"HOSTCFG#{region}")}
    out = []
    for i in insts:
        iid = i["InstanceId"]
        tags = {t["Key"]: t["Value"] for t in i.get("Tags", [])}
        s = ssm.get(iid)
        prof = (i.get("IamInstanceProfile") or {}).get("Arn")
        if s and s["ping"] == "Online":
            ssm_state = "online"
        elif s:
            ssm_state = "offline"
        elif not prof:
            ssm_state = "no_profile"
        else:
            ssm_state = "not_registered"
        c = cfgs.get(iid) or {}
        out.append({
            "instance_id": iid, "name": tags.get("Name", ""), "state": i["State"]["Name"],
            "type": i["InstanceType"], "private_ip": i.get("PrivateIpAddress"), "az": i["Placement"]["AvailabilityZone"],
            "platform": (s or {}).get("platform") or i.get("PlatformDetails"), "launch_time": i.get("LaunchTime"),
            "tags": tags, "instance_profile": prof, "ssm": ssm_state, "agent": (s or {}).get("agent"),
            "last_ping": (s or {}).get("last_ping"),
            "sync_enabled": bool(c.get("enabled")), "templates": c.get("templates", []),
            "extra_paths": c.get("extra_paths", []), "last_sync": c.get("last_sync"),
        })
    out.sort(key=lambda x: (x["name"] or "~", x["instance_id"]))
    return out


SSM_HINT = {
    "no_profile": "实例未挂 IAM 角色：可一键挂载平台提供的 LogAuditInstanceProfile（仅含 AmazonSSMManagedInstanceCore）。",
    "not_registered": "已有实例角色但未注册到 SSM：请确认该角色含 AmazonSSMManagedInstanceCore 策略、SSM Agent 已安装运行、"
                      "且可访问 ssm/ssmmessages/ec2messages 端点（NAT 或 VPC 终端节点）。也可在该区域启用"
                      "『默认主机管理配置(DHMC)』免角色注册。",
    "offline": "SSM Agent 曾注册但当前离线：检查实例是否运行、Agent 进程与网络。",
}


def onboard(region, iid, actor):
    """无实例角色的主机挂载平台实例角色。已有角色的不覆盖（客户角色由客户决定）。"""
    v_region(region); v_iid(iid)
    ec2 = client("ec2", region)
    i = ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
    if i.get("IamInstanceProfile"):
        raise ApiError(409, "实例已有 IAM 角色，平台不覆盖。" + SSM_HINT["not_registered"])
    if not INSTANCE_PROFILE_ARN:
        raise ApiError(500, "未配置平台实例角色")
    ec2.associate_iam_instance_profile(IamInstanceProfile={"Arn": INSTANCE_PROFILE_ARN}, InstanceId=iid)
    return {"ok": True, "message": "已挂载 LogAuditInstanceProfile，SSM Agent 通常 5~30 分钟内完成注册（可重启 Agent 加速）。"}


def get_cfg(region, iid):
    return get(f"HOSTCFG#{region}", iid) or {"enabled": False, "templates": [], "extra_paths": []}


def _norm_paths(paths):
    out = []
    for p in paths or []:
        if isinstance(p, str):
            p = {"path": p}
        path = v_log_path(p["path"].strip())
        out.append({"path": path, "logtype": (p.get("logtype") or "custom")[:32], "kind": p.get("kind") or guess_kind(path),
                    "binmode": p.get("binmode") if p.get("binmode") in ("text", "raw", "both") else "text"})
    return out


def guess_kind(path):
    if path.startswith("journal:"):
        return "journal"
    base = path.rsplit("/", 1)[-1]
    if base.startswith("wtmp"):
        return "wtmp"
    if base.startswith("btmp"):
        return "btmp"
    if base.startswith("lastlog"):
        return "lastlog"
    return "file"


def old_name(region, iid):
    try:
        i = client("ec2", region).describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
        return {t["Key"]: t["Value"] for t in i.get("Tags", [])}.get("Name", "")
    except Exception:  # noqa
        return ""


def set_cfg(region, iid, body, actor):
    v_region(region); v_iid(iid)
    tpl_ids = [t for t in body.get("templates", []) if isinstance(t, str)][:20]
    for t in tpl_ids:
        if not get("TEMPLATE", t):
            raise ApiError(400, f"模板不存在: {t}")
    item = {"enabled": bool(body.get("enabled")), "templates": tpl_ids, "extra_paths": _norm_paths(body.get("extra_paths")),
            "name": (body.get("name") or old_name(region, iid))[:128], "region": region, "instance_id": iid, "updated_by": actor, "updated_at": now_iso()}
    old = get(f"HOSTCFG#{region}", iid) or {}
    if old.get("last_sync"):
        item["last_sync"] = old["last_sync"]
    put(f"HOSTCFG#{region}", iid, **item)
    return item


def bulk_enroll(region, body, actor):
    """按标签批量纳管：tag_key/tag_value 匹配的主机套用模板并开启同步。"""
    v_region(region)
    tk, tv = body.get("tag_key"), body.get("tag_value")
    if not tk:
        raise ApiError(400, "需要 tag_key")
    ids = body.get("templates") or []
    hosts = list_hosts(region, tk, tv)
    done = []
    for h in hosts:
        cur = get_cfg(region, h["instance_id"])
        set_cfg(region, h["instance_id"], {"enabled": body.get("enabled", True), "templates": ids or cur.get("templates", []),
                                           "extra_paths": cur.get("extra_paths", []), "name": h["name"]}, actor)
        done.append(h["instance_id"])
    return {"matched": len(done), "instance_ids": done}


def tag_hosts(region, iids, key, value, actor):
    """给主机打标签（便于按标签纳管）。"""
    v_region(region)
    for i in iids:
        v_iid(i)
    if not key or len(key) > 128 or key.lower().startswith("aws:"):
        raise ApiError(400, "非法标签键")
    client("ec2", region).create_tags(Resources=iids, Tags=[{"Key": key, "Value": value or ""}])
    return {"ok": True, "count": len(iids)}


def all_enabled_cfgs():
    """所有开启同步的主机配置（跨区域，扫表前缀）。"""
    from common import table
    from boto3.dynamodb.conditions import Attr
    out, kw = [], {"FilterExpression": Attr("pk").begins_with("HOSTCFG#") & Attr("enabled").eq(True)}
    while True:
        r = table().scan(**kw)
        out += r["Items"]
        if "LastEvaluatedKey" not in r:
            return out
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def remove_cfg(region, iid):
    delete(f"HOSTCFG#{region}", iid)
