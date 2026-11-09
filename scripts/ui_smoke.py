"""Browser smoke test: drives a full booking through the real UI and saves screenshots.

Usage: python scripts/ui_smoke.py [base_url] [out_dir]
"""
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "screenshots")
OUT.mkdir(exist_ok=True)


def bot_count(page):
    return page.locator(".msg--bot:not(.typing)").count()


def say(page, text):
    n = bot_count(page)
    page.fill("#input", text)
    page.press("#input", "Enter")
    page.wait_for_function(f"document.querySelectorAll('.msg--bot:not(.typing)').length > {n}")


def run():
    errors = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1366, "height": 860})
        page.on("console", lambda m: m.type == "error" and errors.append(m.text))
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE)
        expect(page.locator(".msg--bot").first).to_contain_text("welcome")
        page.screenshot(path=OUT / "1-welcome.png")

        page.get_by_role("button", name="I have tooth pain").click()
        page.wait_for_function("document.querySelectorAll('.msg--bot:not(.typing)').length > 1")
        for m in ["Rahul Sharma", "12345", "98765 43210", "rahul@gmial.com"]:
            say(page, m)
        page.get_by_role("button", name="rahul@gmail.com").click()
        page.wait_for_timeout(400)
        page.get_by_role("button", name="Severe (7–10)").click()
        page.wait_for_timeout(400)
        page.get_by_role("button", name="Yes").click()
        page.wait_for_timeout(500)
        page.screenshot(path=OUT / "2-urgent-slots.png")
        page.locator("#chips .chip").first.click()
        expect(page.locator(".appt--pending")).to_be_visible()
        page.screenshot(path=OUT / "3-review-card.png")
        page.get_by_role("button", name="Yes, confirm").click()
        expect(page.locator(".appt--confirmed")).to_be_visible()
        expect(page.locator(".appt--confirmed .appt__ref")).to_contain_text("BSD-")
        expect(page.get_by_role("link", name="Download .ics")).to_be_visible()
        page.wait_for_timeout(800)  # let smooth-scroll settle
        at_bottom = page.evaluate("(() => { const l = document.querySelector('#log'); return l.scrollHeight - l.scrollTop - l.clientHeight < 4; })()")
        assert at_bottom, "chat log did not scroll to the newest message"
        page.screenshot(path=OUT / "4-confirmed.png", full_page=False)

        m = b.new_page(viewport={"width": 390, "height": 844}, is_mobile=True)
        m.goto(BASE)
        expect(m.locator(".msg--bot").first).to_be_visible()
        m.screenshot(path=OUT / "5-mobile.png")
        overflow = m.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        assert not overflow, "horizontal scroll on mobile"

        d = b.new_page(viewport={"width": 1366, "height": 900})
        d.goto(BASE + "/dashboard")
        d.fill("input[type=password]", "demo")
        d.press("input[type=password]", "Enter")
        expect(d.locator(".lead").first).to_be_visible()
        d.locator(".lead summary").first.click()
        d.screenshot(path=OUT / "6-dashboard.png", full_page=True)
        b.close()
    assert not errors, errors
    print("UI smoke OK ->", OUT.resolve())


if __name__ == "__main__":
    run()
