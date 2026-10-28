"""UI 视觉验收脚本（仅开发自检用，不进入交付依赖）。

用系统 Edge 无头模式打开本应用，依次截取：首页 / 载入示例后 / 提问出图后。
运行： .venv/Scripts/python.exe scripts/ui_shots.py
"""

from __future__ import annotations

import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "eval" / "ui_shots"
OUT.mkdir(parents=True, exist_ok=True)
URL = "http://127.0.0.1:7860/"


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900},
                                device_scale_factor=1)
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        page.screenshot(path=str(OUT / "01_home.png"))

        # 载入示例
        page.get_by_role("tab", name="示例").click()
        page.wait_for_timeout(400)
        page.get_by_role("button", name="加载").click()
        page.wait_for_timeout(4500)
        page.screenshot(path=str(OUT / "02_loaded.png"))

        # 提问（带图，验证图表视图自动切换）
        composer = page.locator(".composer textarea")
        composer.click()
        composer.fill("各大区销售额总和是多少？画成柱状图")
        composer.press("Enter")
        page.wait_for_timeout(22000)
        page.screenshot(path=str(OUT / "03_chat.png"))

        # 面板：预览 / 反思
        page.get_by_text("反思", exact=True).click()
        page.wait_for_timeout(700)
        page.screenshot(path=str(OUT / "04_reflection.png"))

        # 侧栏收起后的效果（活动栏“数据源”按钮）
        page.locator(".rail-btn").first.click()
        page.wait_for_timeout(700)
        page.screenshot(path=str(OUT / "05_sidebar_hidden.png"))

        metrics = page.evaluate(
            """() => {
              const r = (s) => { const e = document.querySelector(s); if (!e) return null;
                const b = e.getBoundingClientRect(); return {top: Math.round(b.top), h: Math.round(b.height), w: Math.round(b.width)}; };
              return { vh: window.innerHeight, scrollY: window.scrollY,
                       titlebar: r('.ide-titlebar'), shell: r('.ide-shell'), status: r('.statusbar'),
                       rail: r('.ide-rail'), sidebar: r('.ide-sidebar'), editor: r('.ide-editor'),
                       panel: r('.ide-panel'), chatRows: document.querySelectorAll('#chat .message-row').length };
            }"""
        )
        print("metrics:", metrics)
        print("console errors:", errors[:5])
        browser.close()
        print("screenshots ->", OUT)


if __name__ == "__main__":
    main()
