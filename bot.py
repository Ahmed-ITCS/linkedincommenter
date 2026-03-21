import asyncio
import hashlib
import random
import sqlite3
import os
import logging
import sys
from datetime import datetime
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from google import genai
from google.api_core.exceptions import ResourceExhausted
from openai import AsyncOpenAI

load_dotenv()

# ─────────────────────────────────────────────
# Logging — stdout (captured by Flask) + file
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
EMAIL       = os.getenv("LINKEDIN_EMAIL")
PASSWORD    = os.getenv("LINKEDIN_PASSWORD")
LLM_API_KEY = os.getenv("LLM_API_KEY")
USE_GEMINI  = os.getenv("USE_GEMINI", "true").lower() == "true"
STATE_FILE  = "linkedin_state.json"
DB_FILE     = "commented_posts.db"

# ─────────────────────────────────────────────
# Gemini key rotation
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
    return GEMINI_KEYS[_gemini_key_idx] if GEMINI_KEYS else None

def rotate_gemini_key() -> str | None:
    global _gemini_key_idx
    _gemini_key_idx += 1
    if _gemini_key_idx >= len(GEMINI_KEYS):
        log.error("🔴 All Gemini API keys exhausted")
        return None
    log.warning(f"🔁 Rotated to Gemini key #{_gemini_key_idx + 1}")
    return GEMINI_KEYS[_gemini_key_idx]

# ─────────────────────────────────────────────
# Groq client
# ─────────────────────────────────────────────
groq_client = (
    AsyncOpenAI(api_key=LLM_API_KEY, base_url="https://api.groq.com/openai/v1")
    if LLM_API_KEY else None
)

# ─────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("CREATE TABLE IF NOT EXISTS commented (urn TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE IF NOT EXISTS commented_hashes (hash TEXT PRIMARY KEY)")
    # Store comment text + post snippet for the dashboard
    conn.execute("""
        CREATE TABLE IF NOT EXISTS comment_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            urn       TEXT,
            post_text TEXT,
            comment   TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()
    log.info(f"🗄️  Database initialised: {DB_FILE}")

def _content_hash(text: str) -> str:
    return hashlib.md5(text.strip()[:500].encode()).hexdigest()

def is_already_commented(urn: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    exists = conn.execute("SELECT 1 FROM commented WHERE urn=?", (urn,)).fetchone()
    conn.close()
    return exists is not None

def is_content_already_commented(text: str) -> bool:
    h = _content_hash(text)
    conn = sqlite3.connect(DB_FILE)
    exists = conn.execute("SELECT 1 FROM commented_hashes WHERE hash=?", (h,)).fetchone()
    conn.close()
    return exists is not None

def mark_as_commented(urn: str, text: str = "", comment: str = ""):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("INSERT OR IGNORE INTO commented (urn) VALUES (?)", (urn,))
    if text:
        h = _content_hash(text)
        conn.execute("INSERT OR IGNORE INTO commented_hashes (hash) VALUES (?)", (h,))
    if comment:
        conn.execute(
            "INSERT INTO comment_log (urn, post_text, comment) VALUES (?, ?, ?)",
            (urn, text[:300], comment)
        )
    conn.commit()
    conn.close()

# ─────────────────────────────────────────────
# LLM
# ─────────────────────────────────────────────
async def generate_comment(post_text: str) -> str:
    prompt = (
        "you are a software engineer, you work in backend but can handle a bit of frontend and devops, "
        "aspire to be a solution architect. Write a short (1-2 sentences), professional, human-sounding "
        "LinkedIn comment. Add value or show genuine interest. No emojis."
    )
    full_prompt = f"{prompt}\n\nPost: {post_text[:700]}"

    if USE_GEMINI:
        while True:
            key = current_gemini_key()
            if key is None:
                log.error("❌ No Gemini keys available — falling back to mock")
                break
            try:
                log.debug(f"🤖 Calling Gemini key #{_gemini_key_idx + 1}")
                client = genai.Client(api_key=key)
                response = client.models.generate_content(
                    model="gemini-3.1-flash-lite-preview",
                    contents=full_prompt
                )
                comment = response.text.strip()
                log.info(f"🤖 Gemini generated comment ({len(comment)} chars)")
                return comment
            except ResourceExhausted as e:
                log.warning(f"⚠️  Gemini key #{_gemini_key_idx + 1} rate limited (429): {e}")
                if rotate_gemini_key() is None:
                    break
            except Exception as e:
                log.error(f"❌ Gemini error: {e}")
                break

    elif groq_client:
        try:
            resp = await groq_client.chat.completions.create(
                model="llama-3.1-70b-versatile",
                messages=[{"role": "user", "content": full_prompt}],
                max_tokens=80,
                temperature=0.7
            )
            comment = resp.choices[0].message.content.strip()
            log.info(f"🤖 Groq generated comment ({len(comment)} chars)")
            return comment
        except Exception as e:
            log.error(f"❌ Groq error: {e}")

    log.warning("⚠️  Using mock comment")
    mocks = [
        "Great insights! Thanks for sharing this perspective.",
        "This is really valuable. Appreciate the post!",
        "Interesting point — looking forward to more content like this.",
        "Well said! This resonates with my experience.",
        "Thanks for breaking this down so clearly!",
        "Excellent analysis, gives me a lot to think about.",
        "Really appreciate you sharing this.",
        "Spot on — great work putting this together."
    ]
    return mocks[len(post_text.strip()) % len(mocks)]

# ─────────────────────────────────────────────
# Playwright helpers
# ─────────────────────────────────────────────
async def get_post_text(post) -> str:
    selectors = [
        '.feed-shared-update-v2__description .break-words',
        '.feed-shared-text .break-words',
        '.feed-shared-update-v2__description span[dir="ltr"]',
        '.update-components-text span[dir="ltr"]',
        '.feed-shared-inline-show-more-text span[dir="ltr"]',
    ]
    for selector in selectors:
        elem = post.locator(selector).first
        if await elem.count():
            text = await elem.inner_text(timeout=3000)
            if text and len(text.strip()) >= 40:
                return text.strip()
    return ""

async def submit_comment(page, comment_box, post) -> bool:
    submit_btn = post.locator('button.comments-comment-box__submit-button--cr').first
    if not await submit_btn.count():
        submit_btn = post.locator('button[class*="submit-button"]').first
    if not await submit_btn.count():
        submit_btn = page.locator('button.comments-comment-box__submit-button--cr').first
    try:
        await submit_btn.wait_for(state="visible", timeout=5000)
        is_disabled = await submit_btn.get_attribute("disabled")
        if is_disabled is not None:
            await comment_box.dispatch_event("input")
            await page.wait_for_timeout(1000)
            is_disabled = await submit_btn.get_attribute("disabled")
            if is_disabled is not None:
                log.warning("⚠️  Submit button still disabled — aborting")
                return False
        await submit_btn.click()
        log.info("✅ Submit button clicked")
        await page.wait_for_timeout(4000)
        return True
    except Exception as e:
        log.error(f"❌ Submit error: {e}")
        return False

# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
async def run():
    log.info("=" * 60)
    log.info("🚀 LinkedIn bot starting up")
    log.info(f"   LLM provider : {'Gemini' if USE_GEMINI else 'Groq' if groq_client else 'Mock'}")
    if USE_GEMINI:
        log.info(f"   Gemini keys  : {len(GEMINI_KEYS)} loaded")
    log.info(f"   State file   : {STATE_FILE}")
    log.info(f"   DB file      : {DB_FILE}")
    log.info("=" * 60)

    init_db()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        log.info("🌐 Chromium launched (headless)")

        if os.path.exists(STATE_FILE):
            context = await browser.new_context(
                storage_state=STATE_FILE,
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            log.info("✅ Loaded saved LinkedIn session")
        else:
            log.info("🔑 No state file — performing fresh login")
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            await page.goto("https://www.linkedin.com/login")
            await page.fill('input[name="session_key"]', EMAIL)
            await page.fill('input[name="session_password"]', PASSWORD)
            await page.click('button[type="submit"]')
            log.info("⏳ Waiting 15s for auth...")
            await page.wait_for_timeout(15000)
            await context.storage_state(path=STATE_FILE)
            log.info(f"✅ Login done — session saved to {STATE_FILE}")

        page = await context.new_page()
        log.info("📡 Navigating to feed")
        await page.goto("https://www.linkedin.com/feed/")

        try:
            await page.wait_for_selector('div[data-urn^="urn:li:activity:"]', timeout=15000)
            log.info("✅ Feed loaded")
        except Exception:
            log.error("❌ Feed not found — not logged in or DOM changed")
            screenshot_path = f"debug_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            await page.screenshot(path=screenshot_path)
            log.error(f"📸 Screenshot: {screenshot_path}")

        round_number = 0

        while True:
            round_number += 1
            log.info("─" * 50)
            log.info(f"🔄 Round #{round_number} started")

            commented_this_round = 0
            max_comments_per_round = 6
            all_urns = []
            seen_urns = set()

            post_containers = await page.locator('div[data-urn^="urn:li:activity:"]').all()
            for post in post_containers:
                urn = await post.get_attribute("data-urn")
                if urn and not is_already_commented(urn) and urn not in seen_urns:
                    seen_urns.add(urn)
                    all_urns.append(urn)

            log.info(f"📊 {len(post_containers)} posts found, {len(all_urns)} unseen")

            for urn in all_urns:
                if commented_this_round >= max_comments_per_round:
                    log.info(f"🛑 Max comments/round reached ({max_comments_per_round})")
                    break
                if is_already_commented(urn):
                    continue

                short_urn = urn[:50]
                log.info(f"👀 Processing {short_urn}...")

                try:
                    post = page.locator(f'div[data-urn="{urn}"]').first
                    if not await post.count():
                        log.warning(f"⏭️  {short_urn} not in DOM — skipping")
                        continue

                    post_text = await get_post_text(post)
                    if not post_text:
                        log.info(f"⏭️  {short_urn} — no text, skipping")
                        mark_as_commented(urn)
                        continue

                    if is_content_already_commented(post_text):
                        log.info(f"⏭️  {short_urn} — duplicate content, skipping")
                        mark_as_commented(urn)
                        continue

                    log.info(f"✍️  Generating comment for {short_urn}")
                    comment = await generate_comment(post_text)
                    log.info(f"💬 Comment: \"{comment}\"")

                    # Save to DB with comment text for dashboard
                    mark_as_commented(urn, post_text, comment)

                    await post.scroll_into_view_if_needed()
                    await page.wait_for_timeout(500)

                    await post.locator('button[aria-label="Comment"]').first.click(timeout=8000)
                    await page.wait_for_timeout(1500)

                    comment_box = post.locator('div[role="textbox"][contenteditable="true"]').first
                    await comment_box.click()
                    await page.wait_for_timeout(500)
                    await comment_box.fill("")
                    await comment_box.type(comment, delay=random.randint(15, 35))
                    await comment_box.dispatch_event("input")
                    await comment_box.dispatch_event("change")
                    await page.wait_for_timeout(2000)

                    submitted = await submit_comment(page, comment_box, post)
                    if submitted:
                        commented_this_round += 1
                        log.info(f"✅ Commented on {short_urn} ({commented_this_round}/{max_comments_per_round})")
                    else:
                        log.warning(f"⚠️  Could not submit for {short_urn}")

                    wait_secs = random.uniform(18, 38)
                    log.info(f"⏳ Waiting {wait_secs:.1f}s...")
                    await asyncio.sleep(wait_secs)

                except Exception as e:
                    log.error(f"❌ Error on {short_urn}: {e}", exc_info=True)
                    continue

            log.info(f"✅ Round #{round_number} done — {commented_this_round} comments made")
            await page.evaluate("window.scrollBy(0, 700)")
            await asyncio.sleep(random.randint(25, 45))
            log.info("😴 Sleeping 30 minutes before next round...")
            await asyncio.sleep(30 * 60)

            # Refresh the feed page so LinkedIn loads new posts
            log.info("🔄 Refreshing feed page...")
            await page.goto("https://www.linkedin.com/feed/")
            try:
                await page.wait_for_selector('div[data-urn^="urn:li:activity:"]', timeout=15000)
                log.info("✅ Feed refreshed successfully")
            except Exception:
                log.error("❌ Feed reload failed — will retry next round")


asyncio.run(run())