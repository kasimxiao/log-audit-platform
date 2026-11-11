#!/usr/bin/env python3
"""docs/solution-overview.html → docs/solution-overview.md + docs/img/*.svg（GitHub 可直接渲染；HTML 为编辑源）。
用法：python3 scripts/export_solution_md.py   （需本机 playwright + chromium，用于渲染 JS 生成的时序图）"""
import os, re
from playwright.sync_api import sync_playwright
D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs") + "/"
NAMES = ["architecture", "seq-daily-sync", "seq-live-query", "seq-athena-query", "seq-nlq"]
os.makedirs(D + "img", exist_ok=True)
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(viewport={"width": 1240, "height": 900})
    pg.goto("file://" + os.path.abspath(D + "solution-overview.html")); pg.wait_for_timeout(600)
    svgs = pg.eval_on_selector_all(".fig svg", "els => els.map(e => e.outerHTML)")
    body = pg.eval_on_selector(".wrap", "e => e.innerHTML")
    b.close()
for name, svg in zip(NAMES, svgs):
    svg = svg.replace("<svg ", '<svg xmlns="http://www.w3.org/2000/svg" ', 1).replace('width="100%"', 'width="1120"', 1)
    svg = svg.replace("<defs>", "<defs><style>text{font-family:-apple-system,'PingFang SC','Microsoft YaHei','Noto Sans CJK SC',sans-serif}</style>", 1)
    svg = re.sub(r'(<svg [^>]*>)', r'\1<rect width="100%" height="100%" fill="#fff"/>', svg, count=1) if name != "architecture" else svg.replace("<svg ", '<svg style="background:#fff" ', 1)
    open(D + f"img/{name}.svg", "w").write(svg)
i = 0
def fig(m):
    global i
    cap = re.search(r'<div class="cap">(.*?)</div>', m.group(0), re.S).group(1)
    out = f'<p><img src="img/{NAMES[i]}.svg" alt="{re.sub("<[^>]+>", "", cap)}" width="100%"></p>\n<p align="center"><sub>{cap}</sub></p>'
    i += 1
    return out
body = re.sub(r'<div class="fig">.*?</div>\s*</div>', fig, body, flags=re.S)
body = re.sub(r'<div class="toc">.*?</div>', lambda m: "<p><b>目录</b>：" + " · ".join(f'<a href="#{h}">{t}</a>' for h, t in re.findall(r'href="#([^"]+)">([^<]+)</a>', m.group(0))) + "</p>", body, flags=re.S)
body = re.sub(r'<div class="kv">(.*?)</div>\s*(?=<table>)', lambda m: "<table><tr>" + "".join(f"<td><b>{a}</b><br>{b}</td>" for a, b in re.findall(r"<b>(.*?)</b><span>(.*?)</span>", m.group(1))) + "</tr></table>\n", body, count=1, flags=re.S)
body = re.sub(r'<div class="note">(.*?)</div>', r'<blockquote>\1</blockquote>', body, flags=re.S)
body = re.sub(r'<span class="tag">(.*?)</span>', r'<code>\1</code> ', body)
body = re.sub(r'<p class="sub">(.*?)</p>', r'<p><i>\1</i></p>', body)
open(D + "solution-overview.md", "w").write("<!-- 由 docs/solution-overview.html 生成（scripts/export_solution_md.py），请改 HTML 后重新导出 -->\n" + body.strip() + "\n")
print(f"导出 {i} 张图 → docs/img/，docs/solution-overview.md")
