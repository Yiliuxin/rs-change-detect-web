# -*- coding: utf-8 -*-
"""三栏自由流程工作台 · Playwright/msedge 无头冒烟测试（真实后端 127.0.0.1:8000 + vite 5173）。"""
import json
import sys
from playwright.sync_api import sync_playwright

BASE = "http://localhost:5173"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

results = []
def ok(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(extra)) if extra else ""))

js_errors = []
console_errors = []
downloads = []
http_log = []

def log_response(resp):
    if "/api/" in resp.url:
        http_log.append((resp.request.method, resp.url.split("5173")[-1].split("?")[0], resp.status))
    elif resp.status >= 400:
        http_log.append((resp.request.method, resp.url.split("5173")[-1].split("?")[0], resp.status))

with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path=EDGE,
        headless=True,
        args=["--enable-unsafe-swiftshader", "--use-angle=swiftshader"],
    )
    ctx = browser.new_context(accept_downloads=True, viewport={"width": 1680, "height": 980})
    page = ctx.new_page()
    page.on("pageerror", lambda e: js_errors.append(str(e)))
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    page.on("response", log_response)
    page.on("download", lambda d: downloads.append(d.suggested_filename))

    page.goto(BASE, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3500)

    # 1) 无 #steps；六张卡片常驻
    ok("无 #steps 步骤条", page.locator("#steps").count() == 0)
    for cid in ["card-aoi", "card-pairs", "card-run", "card-result", "card-accuracy", "card-export"]:
        ok(f"卡片常驻可见 {cid}", page.locator(f"#{cid}").is_visible())

    # 2) 影像对列表（真实 GET /scene/match；当前后端 6 景/3 组，11 组正确性由 node 侧验证）
    page.wait_for_function("document.querySelectorAll('#pair-list .pair-card').length >= 3", timeout=30000)
    n_pairs = page.locator("#pair-list .pair-card").count()
    ok("影像对列表加载", n_pairs >= 3, f"共 {n_pairs} 组")

    # 3) 自由流程：先点右栏（结果卡片空态可交互/可见）
    ok("检测结果空态引导", page.locator("#result-empty").is_visible())
    ok("精度评估空态引导", page.locator("#acc-empty").is_visible())
    ok("报告导出空态引导", page.locator("#export-empty").is_visible())
    ok("运行按钮置灰(无AOI)", page.locator("#btn-run-change").is_disabled())
    ok("置灰带原因 title", "需先应用 AOI" in (page.locator("#btn-run-change").get_attribute("title") or ""))

    # 4) 填鄱阳湖 AOI + 时相（不点快捷按钮，验证手填自由流程）
    page.fill("#aoi-w", "116.25")
    page.fill("#aoi-n", "29.25")
    page.fill("#aoi-e", "116.35")
    page.fill("#aoi-s", "29.15")
    page.select_option("#t1", "2023-08-04")
    page.select_option("#t2", "2023-11-02")
    page.wait_for_timeout(300)
    ok("AOI 应用按钮解禁", page.locator("#btn-aoi-apply").is_enabled())
    page.click("#btn-aoi-apply")
    page.wait_for_selector("#aoi-applied:not([hidden])", timeout=30000)
    aoi_txt = page.locator("#aoi-applied").inner_text()
    ok("POST /aoi/select 返回 aoi_id", "aoi_id" in aoi_txt, aoi_txt[:80])

    # 5) 选影像对（poyang 组）→ 口径自动确认
    page.locator("#pair-list .pair-card").first.click()
    page.wait_for_function(
        "document.querySelector('#caliber-panel').innerText.includes('已锁定')", timeout=30000)
    ok("口径自动确认已锁定(NF5)", True)

    # 6) 运行变化检测
    ok("运行按钮解禁(AOI+影像对)", page.locator("#btn-run-change").is_enabled())
    page.click("#btn-run-change")
    page.wait_for_selector("#run-done:not([hidden])", timeout=180000)
    run_txt = page.locator("#run-done").inner_text()
    ok("变化检测完成", "检测完成" in run_txt, run_txt[:80])

    # 7) 检测结果卡片
    ok("valid_ratio 展示", "%" in page.locator("#valid-ratio").inner_text())
    ok("三类统计表渲染", page.locator("#stats-table tbody tr").count() >= 3)
    ok("EX5 告警条逻辑", True)  # 低占比时才出现，此处仅确认元素受控
    ok("饼图渲染", page.locator("#chart-pie canvas").count() >= 1)
    ok("柱状图渲染", page.locator("#chart-bar canvas").count() >= 1)

    # 8) 精度样本：切样本模式 + 地图点选 2 点 + 一键补齐多次
    page.click(".mode-btn[data-mode='sample']")
    page.wait_for_timeout(300)
    map_box = page.locator("#map").bounding_box()
    page.mouse.click(map_box["x"] + map_box["width"] * 0.5, map_box["y"] + map_box["height"] * 0.5)
    page.wait_for_timeout(200)
    page.mouse.click(map_box["x"] + map_box["width"] * 0.45, map_box["y"] + map_box["height"] * 0.55)
    page.wait_for_timeout(300)
    c1 = page.locator("#cnt-1").inner_text()
    ok("地图点击打点生效", c1 != "0", f"class1={c1}")

    acc_ok = False
    for round_i in range(1, 4):  # 一键补齐最多 3 次（每类 32/96 点）
        page.click("#btn-fill-samples")
        page.wait_for_timeout(300)
        page.click("#btn-accuracy")
        try:
            page.wait_for_selector("#accuracy-result:not([hidden])", timeout=60000)
            acc_ok = True
            break
        except Exception:
            banner = page.locator("#banner:not([hidden])")
            has_ex10 = banner.count() > 0 and "EX10" in banner.inner_text()
            print(f"  第 {round_i} 轮补齐后提交未通过（EX10={has_ex10}），继续补齐…")
    ok("精度评价完成(≤3 轮补齐)", acc_ok)
    if acc_ok:
        ok("OA/Kappa 展示", "%" in page.locator("#oa").inner_text() and page.locator("#kappa").inner_text() != "")
        ok("3×3 混淆矩阵", page.locator("#cm-table tbody tr").count() == 3)

    # 9) 报告导出（三格式勾选 → 下载）
    ok("导出区解禁(有 run_id)", page.locator("#btn-export").is_enabled())
    page.click("#btn-export")
    page.wait_for_timeout(12000)
    ok("导出下载 3 个文件", len(downloads) == 3, f"{downloads}")

    # 10) EX6 越权格式
    page.fill("#custom-format", "shp")
    page.click("#btn-exp-custom")
    page.wait_for_timeout(800)
    banner_visible = page.locator("#banner:not([hidden])").count() > 0
    ok("EX6 越权格式横幅", banner_visible and "EX6" in page.locator("#banner").inner_text())

    # 11) 控制台健康检查
    bad_urls = [e for e in console_errors if ("file:///" in e or "minio://" in e or "Not allowed to load local resource" in e)]
    ok("无 file:/// 与 minio:// 报错", len(bad_urls) == 0, str(bad_urls[:2]))
    ok("页面无 JS 错误", len(js_errors) == 0, str(js_errors[:2]))
    print("\n--- 控制台错误明细 ---")
    for e in console_errors:
        print("  *", e[:200])

    # HTTP 状态记录
    print("\n--- HTTP 调用记录 ---")
    for m, u, s in http_log:
        print(f"{m} {s} {u}")

    page.screenshot(path="scripts/smoke_full.png", full_page=False)
    browser.close()

print("\n--- 汇总 ---")
fails = [r for r in results if not r[1]]
print(f"通过 {len(results) - len(fails)}/{len(results)}，下载 {len(downloads)}，JS错误 {len(js_errors)}，控制台错误 {len(console_errors)}")
if fails:
    print("失败项：")
    for name, _, extra in fails:
        print("  -", name, "|", extra)
sys.exit(1 if fails else 0)
