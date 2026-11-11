"""以 admin 身份调用平台 API（经 CloudFront），供冒烟脚本复用。"""
import json, os, time, urllib.request
import boto3
REGION = os.environ.get("LA_REGION", "ap-northeast-1")
CRED = dict(l.strip().split("=", 1) for l in open(os.path.expanduser("~/.log-audit-admin.txt")) if "=" in l)
cfn = boto3.client("cloudformation", region_name=REGION)
P = {o["OutputKey"]: o["OutputValue"] for o in cfn.describe_stacks(StackName="LogAuditPlatform")["Stacks"][0]["Outputs"]}
_tok = boto3.client("cognito-idp", region_name=REGION).initiate_auth(AuthFlow="USER_PASSWORD_AUTH", ClientId=P["UserPoolClientId"],
    AuthParameters={"USERNAME": CRED["username"], "PASSWORD": CRED["password"]})["AuthenticationResult"]["IdToken"]

def call(method, path, body=None):
    req = urllib.request.Request(P["Url"] + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": _tok, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r: return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> {e.code} {e.read()[:300]}")

def query(start):
    for _ in range(200):
        r = call("GET", f"/api/query/{start['query_id']}")
        if r["state"] in ("SUCCEEDED", "FAILED", "CANCELLED"): return r
        time.sleep(1.2)
