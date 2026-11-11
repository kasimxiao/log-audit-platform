"""以 admin 登录平台，逐页截图到 /tmp/la-shots（验证页面现象）。"""
import os, re, time
from playwright.sync_api import sync_playwright
C = dict(l.strip().split("=", 1) for l in open(os.path.expanduser("~/.log-audit-admin.txt")) if "=" in l)
OUT = "/tmp/la-shots"
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(viewport={"width": 1500, "height": 1000}, device_scale_factor=1)
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(C["url"]); pg.fill("#lu", C["username"]); pg.fill("#lp", C["password"]); pg.click("#lf button")
    pg.wait_for_selector("nav"); 
    def shot(name, full=True): pg.screenshot(path=f"{OUT}/{name}.png", full_page=full); print("shot", name)
    pg.goto(C["url"] + "#hosts"); pg.wait_for_selector("#hList table", timeout=30000); time.sleep(1.5); shot("01-hosts")
    pg.click("text=同步配置 >> nth=0"); time.sleep(1.5); shot("02-host-config", False); pg.click("[data-x]")
    pg.goto(C["url"] + "#templates"); time.sleep(2.5); shot("03-templates")
    pg.goto(C["url"] + "#sync"); pg.wait_for_selector("#sList table", timeout=30000); shot("04-sync")
    pg.goto(C["url"] + "#hostlogs"); time.sleep(3); pg.select_option("#lHost", label=re.compile("la-ubuntu-02")) if False else None
    opts = pg.eval_on_selector_all("#lHost option", "o=>o.map(x=>[x.value,x.textContent])")
    ubu = [v for v, t in opts if "la-web-01" in t][0]; pg.select_option("#lHost", ubu)
    pg.click("#lLs"); pg.wait_for_selector("#lFiles table", timeout=60000)
    pg.fill("#lPath", "/var/log/secure"); pg.select_option("#lPreset", index=1); pg.click("#lGo"); pg.wait_for_selector("#lOut pre, #lOut div", timeout=60000); time.sleep(1); shot("05-live")
    pg.click("text=历史检索（S3）"); time.sleep(1); pg.fill("#fKw", "Failed password"); pg.click("#fGo"); pg.wait_for_selector("#fOut table", timeout=60000); shot("06-history")
    pg.goto(C["url"] + "#scenarios"); time.sleep(2); pg.click("[data-run=success_after_fail]") if False else pg.click("[data-run=ssh_bruteforce]")
    pg.wait_for_selector("#scOut table, #scOut .muted", timeout=60000); time.sleep(1); shot("07-scenarios")
    pg.goto(C["url"] + "#alb"); time.sleep(2); pg.select_option("#aSrc", "demo"); pg.fill("#aFrom", "2026-11-01"); pg.fill("#aTo", "2026-11-03"); pg.click("#aGo")
    pg.wait_for_selector("#rUa table", timeout=120000); time.sleep(3); shot("08-alb-dash")
    pg.goto(C["url"] + "#cloudtrail"); time.sleep(2); pg.click("text=SSM 会话/远程命令"); pg.wait_for_selector("#cOut table, #cOut .muted", timeout=90000); shot("09-cloudtrail")
    pg.goto(C["url"] + "#users"); pg.wait_for_selector("#uList table", timeout=30000); shot("10-users")
    pg.goto(C["url"] + "#audit"); pg.wait_for_selector("#auOut table", timeout=30000); shot("11-audit")
    pg.goto(C["url"] + "#settings"); time.sleep(5); shot("12-settings")
    pg.goto(C["url"] + "#nlq"); time.sleep(1); pg.fill("#q", "11月份以来 ALB 演示数据里 5xx 最多的是哪个接口、哪个时段？"); pg.click("#ask")
    pg.wait_for_selector(".msg.a .trace", timeout=180000); time.sleep(1); shot("13-nlq")
    for t in ["ds", "dims", "run"]:
        pg.goto(C["url"] + f"/eval.html#{t}"); time.sleep(3); shot(f"14-eval-{t}")
    print("page errors:", errs)
    b.close()
