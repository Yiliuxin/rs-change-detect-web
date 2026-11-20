# -*- coding: utf-8 -*-
"""批次 K 浏览器实测：真实 run 历史回看 → 0.8× / 1.2× 滑块 → 批次 E 锚点 + 耗时。

前置：后端 8010（AUTH_DISABLED=1 以便玩家读取无归属的真实历史 run）+ vite 5173。
"""
import json
import time
import urllib.request
import uuid

from playwright.sync_api import sync_playwright

BASE = "http://localhost:5173"
API = "http://127.0.0.1:8010/api/v1"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
RID = "0cdc51cc-06c8-44d0-952b-e4d4f8a25d1e"
ANCHORS = {
    "0.8": ["2419.22", "175.61", "4201.31"],
    "1.2": ["2024.28", "52.88", "4718.98"],
}


def api_post(path, body):
    req = urllib.request.Request(
        API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def areas(page):
    return [
        page.locator("#stats-table tbody tr").nth(i).locator("td").nth(2).inner_text().strip()
        for i in range(3)
    ]


username = "qa_k_" + uuid.uuid4().hex[:8]
token = api_post("/auth/register", {"username": username, "password": "pw123456"})["token"]

with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path=EDGE, headless=True,
        args=["--enable-unsafe-swiftshader", "--use-angle=swiftshader"])
    ctx = browser.new_context(viewport={"width": 1680, "height": 980})
    page = ctx.new_page()
    js_errors = []
    http_log = []
    page.on("pageerror", lambda e: js_errors.append(str(e)))
    page.on("response", lambda r: http_log.append(
        (r.request.method, r.status, r.url.split("5173")[-1].split("?")[0]))
        if "/api/" in r.url else None)
    page.goto(BASE, wait_until="domcontentloaded", timeout=30000)
    page.evaluate("(t)=>localStorage.setItem('rst_token',t)", token)
    page.reload(wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)
    print("url:", page.url)

    page.fill("#hist-q", "0cdc51cc")
    page.wait_for_selector('.hist-item[data-rid^="0cdc51cc"]', timeout=30000)
    page.locator('.hist-item[data-rid^="0cdc51cc"]').first.click()
    page.wait_for_timeout(2000)
    print("hist-detail:", page.locator("#hist-detail").inner_text()[:400])
    print("http:", http_log[-6:])
    page.wait_for_selector("#hist-load:not([hidden])", timeout=30000)
    page.click("#hist-load")
    page.wait_for_selector("#result-body:not([hidden])", timeout=30000)
    page.wait_for_selector("#reclass-bar:not([hidden])", timeout=10000)

    rid = page.locator("#runid-val").inner_text().strip()
    base = areas(page)
    print("real run:", rid)
    print("baseline T:", page.locator("#threshold-val").inner_text(), "areas:", base)
    assert rid == RID, rid

    sens = api_post("/result/sensitivity", {"run_id": rid})
    anchors = {}
    for row in sens["sweep"]:
        if float(row["scale"]) in (0.8, 1.2):
            anchors[f"{float(row['scale']):.1f}"] = [
                f"{float(a['area_ha']):.2f}" for a in row["areas"]]
    print("batch-E sensitivity anchors:", anchors)
    assert anchors == ANCHORS, anchors

    for scale, expected in anchors.items():
        t0 = time.perf_counter()
        page.evaluate(
            """(v)=>{const s=document.querySelector('#reclass-slider');"""
            """s.value=v;s.dispatchEvent(new Event('input',{bubbles:true}));}""", scale)
        expr = ("()=>{const a=[...document.querySelectorAll('#stats-table tbody tr')]"
                ".map(r=>r.querySelectorAll('td')[2]?.innerText.trim());"
                "return " + "&&".join(f"a[{i}]==='{v}'" for i, v in enumerate(expected)) + ";}")
        page.wait_for_function(expr, timeout=30000)
        elapsed = (time.perf_counter() - t0) * 1000
        got = areas(page)
        print(f"{scale}x T={page.locator('#threshold-val').inner_text()} areas={got} "
              f"elapsed={elapsed:.0f}ms legend={page.locator('#reclass-legend-scale').inner_text()}")
        assert got == expected, (scale, got, expected)
        assert page.locator("#chart-delta-hist canvas").count() >= 1

    print("reset disabled?", page.locator("#btn-reclass-reset").is_disabled())
    page.click("#btn-reclass-reset")
    page.wait_for_timeout(500)
    print("after click:", page.locator("#reclass-scale-val").inner_text(),
          page.locator("#threshold-val").inner_text(), areas(page))
    assert areas(page) == base, (areas(page), base)
    print("after reset T:", page.locator("#threshold-val").inner_text(), "areas:", areas(page))
    print("reclass HTTP:", [x for x in http_log if "reclass" in x[2]])
    print("js_errors:", js_errors)
    browser.close()
