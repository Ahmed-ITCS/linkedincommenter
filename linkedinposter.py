import asyncio
import os
import logging
import sys
from datetime import datetime
from dotenv import load_dotenv
from playwright.async_api import async_playwright

from autopost import maybe_auto_post

load_dotenv()

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
LOG_FILE = "linkedin_bot.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
EMAIL      = os.getenv("LINKEDIN_EMAIL")
PASSWORD   = os.getenv("LINKEDIN_PASSWORD")
STATE_FILE = "linkedin_state.json"

# ─────────────────────────────────────────────
# Gemini key stubs (autopost signature needs
# these callables even in mock/dry-run mode)
# ─────────────────────────────────────────────
def _load_gemini_keys() -> list[str]:
    keys = []
    i = 1
    while True:
        k = os.getenv(f"GEMINI_API_KEY_{i}")
        if not k:
            break
        keys.append(k)
        i += 1
    if not keys:
        fallback = os.getenv("GEMINI_API_KEY")
        if fallback:
            keys.append(fallback)
    return keys

GEMINI_KEYS     = _load_gemini_keys()
_gemini_key_idx = 0

def current_gemini_key() -> str | None:
    if not GEMINI_KEYS:
        return None
    return GEMINI_KEYS[_gemini_key_idx]

def rotate_gemini_key() -> str | None:
    global _gemini_key_idx
    _gemini_key_idx += 1
    if _gemini_key_idx >= len(GEMINI_KEYS):
        return None
    return GEMINI_KEYS[_gemini_key_idx]


# ─────────────────────────────────────────────
# Main — post only, no commenting
# ─────────────────────────────────────────────
async def run():
    log.info("=" * 60)
    log.info("🚀 LinkedIn poster starting (TEST MODE)")
    log.info("   ✏️  Using mock post text")
    log.info("   🛑  Submit is DISABLED — will not actually post")
    log.info("=" * 60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,   # visible so you can see what's happening
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )

        if os.path.exists(STATE_FILE):
            context = await browser.new_context(
                storage_state=STATE_FILE,
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            log.info("✅ Loaded saved LinkedIn session")
        else:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            await page.goto("https://www.linkedin.com/login")
            log.info("📋 Logging in...")
            await page.fill('input[name="session_key"]', EMAIL)
            await page.fill('input[name="session_password"]', PASSWORD)
            await page.click('button[type="submit"]')
            await page.wait_for_timeout(15000)
            await context.storage_state(path=STATE_FILE)
            log.info("✅ Login successful — session saved")

        page = await context.new_page()
        log.info("📡 Navigating to LinkedIn feed")
        await page.goto("https://www.linkedin.com/feed/")

        try:
            await page.wait_for_selector('div[data-urn^="urn:li:activity:"]', timeout=15000)
            log.info("✅ Feed loaded")
        except Exception:
            screenshot_path = f"debug_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            await page.screenshot(path=screenshot_path)
            log.error(f"❌ Feed not found — screenshot saved: {screenshot_path}")

        # ── Single test run of the auto-poster ──
        await maybe_auto_post(
            page,
            current_gemini_key,
            rotate_gemini_key,
            mock_text=True,   # skip Gemini — use hardcoded test post
            dry_run=True,     # open composer + type text but do NOT click Post
        )

        log.info("🏁 Test complete — browser stays open 60s so you can inspect")
        await asyncio.sleep(60)
        await browser.close()


asyncio.run(run())