"""
Headed helper: log in manually, wait until the real feed URL loads, settle, then save
linkedin_state.json. Avoid saving right after submit — LinkedIn often sets cookies after
redirect to /feed/.

  python export_linkedin_state.py

Optional: LINKEDIN_CHROME_CHANNEL=chrome in .env (match bot.py for fewer mismatches).
"""
from __future__ import annotations

import asyncio
import os
import time
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

STATE_FILE = "linkedin_state.json"
FEED_URL = "https://www.linkedin.com/feed/"

VIEWPORT_W, VIEWPORT_H = 1365, 900
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_CHROME_ARGS = (
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    f"--window-size={VIEWPORT_W},{VIEWPORT_H}",
)
_ANTIDETECT_INIT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
"""


def _linkedin_host_ok(host: str) -> bool:
    h = (host or "").lower()
    return "linkedin." in h or h.endswith("linkedin.com")


def _feed_session_ready(url: str) -> bool:
    """True when we are in the logged-in feed area (not login/checkpoint)."""
    try:
        p = urlparse(url or "")
    except Exception:
        return False
    host = (p.netloc or "").lower()
    path = (p.path or "").lower()
    full = f"{host}{path}".lower()

    if not _linkedin_host_ok(host):
        return False
    # Do NOT reject paths containing the substring "uas" — activity URNs often include
    # urn:li:uas:… inside /feed/update/… URLs, which wrongly blocked saving.
    if path.startswith("/login") or "/checkpoint" in path or "checkpoint/lg" in full:
        return False
    return path.startswith("/feed")


async def main() -> None:
    channel = os.getenv("LINKEDIN_CHROME_CHANNEL", "").strip() or None
    launch_kw: dict = {
        "headless": False,
        "args": list(_CHROME_ARGS),
        "ignore_default_args": ["--enable-automation"],
    }
    if channel:
        launch_kw["channel"] = channel

    print("Opening LinkedIn. Log in and complete any 2FA / CAPTCHA in the browser window.")
    print("This script saves when the URL is a feed page (path starts with /feed), not /login.")
    print("If it seems stuck, watch the 'Still waiting' lines — open https://www.linkedin.com/feed/ manually.")
    print()

    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kw)
        context = await browser.new_context(
            viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
            user_agent=CHROME_USER_AGENT,
            locale="en-US",
            timezone_id=os.getenv("LINKEDIN_TZ", "UTC"),
        )
        await context.add_init_script(_ANTIDETECT_INIT)
        page = await context.new_page()
        await page.goto(FEED_URL, wait_until="domcontentloaded", timeout=120000)

        deadline = time.monotonic() + 20 * 60
        last_log = 0.0
        while time.monotonic() < deadline:
            cur = ""
            try:
                cur = page.url or ""
            except Exception:
                pass
            now = time.monotonic()
            if now - last_log >= 10:
                print(f"… Still waiting (current URL): {cur or '(empty)'}")
                last_log = now
            if _feed_session_ready(cur):
                # Let deferred auth cookies settle after SPA / redirect chain.
                await asyncio.sleep(6)
                out = os.path.abspath(STATE_FILE)
                try:
                    await context.storage_state(path=STATE_FILE)
                except OSError as e:
                    print(f"Could not write {STATE_FILE!r}: {e}")
                    raise
                print(f"Saved session to:\n  {out}")
                await browser.close()
                return
            await asyncio.sleep(1)

        print("Timed out (20 min) — URL never matched a /feed… page (see messages above).")
        await browser.close()
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
