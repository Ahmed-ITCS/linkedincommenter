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
from openai import AsyncOpenAI

from zai_llm import ZAI_KEYS, generate_text

load_dotenv()

# ─────────────────────────────────────────────
# Logging setup — writes to both stdout and file
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
EMAIL           = os.getenv("LINKEDIN_EMAIL")
PASSWORD        = os.getenv("LINKEDIN_PASSWORD")
LLM_API_KEY     = os.getenv("LLM_API_KEY")
USE_ZAI         = os.getenv("USE_ZAI", "true").lower() == "true"
STATE_FILE      = "linkedin_state.json"
DB_FILE         = "commented_posts.db"

# ─────────────────────────────────────────────
# Groq client (optional)
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

def mark_as_commented(urn: str, text: str = ""):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("INSERT OR IGNORE INTO commented (urn) VALUES (?)", (urn,))
    if text:
        h = _content_hash(text)
        conn.execute("INSERT OR IGNORE INTO commented_hashes (hash) VALUES (?)", (h,))
    conn.commit()
    conn.close()

# ─────────────────────────────────────────────
# LLM comment generation
# ─────────────────────────────────────────────
async def generate_comment(post_text: str) -> str:
    prompt = (
        "you are a software engineer, you work in backend but can handle a bit of frontend and devops, "
        "aspire to be a solution architect. Write a short (1-2 sentences), professional, human-sounding "
        "LinkedIn comment. Add value or show genuine interest. No emojis."
    )
    full_prompt = f"{prompt}\n\nPost: {post_text[:700]}"

    if USE_ZAI:
        try:
            return await generate_text(full_prompt, max_tokens=80, temperature=0.7)
        except Exception as e:
            log.error(f"❌ Z.ai error: {e}")

    elif groq_client:
        try:
            log.debug("🤖 Calling Groq LLM")
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
            log.error(f"❌ Groq API error: {e}")

    raise RuntimeError(
        "Comment generation failed: configure ZAI_API_KEY (USE_ZAI=true) or LLM_API_KEY for Groq."
    )


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
                log.debug(f"📄 Extracted post text via selector '{selector}' ({len(text.strip())} chars)")
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
        log.debug(f"Submit button disabled attribute: {is_disabled}")

        if is_disabled is not None:
            log.debug("Submit disabled — dispatching input event to re-enable")
            await comment_box.dispatch_event("input")
            await page.wait_for_timeout(1000)
            is_disabled = await submit_btn.get_attribute("disabled")
            if is_disabled is not None:
                log.warning("⚠️  Submit button still disabled — aborting to avoid double comment")
                return False

        await submit_btn.click()
        log.info("✅ Submit button clicked")
        await page.wait_for_timeout(4000)
        return True

    except Exception as e:
        log.error(f"❌ Submit button error: {e}")
        return False


# ─────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────
async def run():
    log.info("=" * 60)
    log.info("🚀 LinkedIn bot starting up")
    log.info(
        f"   LLM provider  : {'Z.ai' if USE_ZAI else 'Groq' if groq_client else 'none (set keys)'}"
    )
    if USE_ZAI:
        log.info(f"   Z.ai keys     : {len(ZAI_KEYS)} loaded")
    log.info(f"   State file    : {STATE_FILE}")
    log.info(f"   DB file       : {DB_FILE}")
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
            log.info("✅ Loaded saved LinkedIn session from state file")
        else:
            log.info("🔑 No state file found — performing fresh login")
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            await page.goto("https://www.linkedin.com/login")
            log.info("📋 Filling login form")
            await page.fill('input[name="session_key"]', EMAIL)
            await page.fill('input[name="session_password"]', PASSWORD)
            await page.click('button[type="submit"]')
            log.info("⏳ Waiting for LinkedIn to authenticate (15s)...")
            await page.wait_for_timeout(15000)
            await context.storage_state(path=STATE_FILE)
            log.info(f"✅ Login successful — session saved to {STATE_FILE}")

        page = await context.new_page()
        log.info("📡 Navigating to LinkedIn feed")
        await page.goto("https://www.linkedin.com/feed/")

        try:
            await page.wait_for_selector('div[data-urn^="urn:li:activity:"]', timeout=15000)
            log.info("✅ Feed loaded successfully")
        except Exception:
            log.error("❌ Feed selector not found — possibly not logged in or LinkedIn changed DOM")
            screenshot_path = f"debug_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            await page.screenshot(path=screenshot_path)
            log.error(f"📸 Debug screenshot saved: {screenshot_path}")

        round_number = 0

        while True:
            round_number += 1
            log.info(f"{'─' * 50}")
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

            log.info(f"📊 Found {len(post_containers)} posts on feed, {len(all_urns)} unseen URNs")

            for urn in all_urns:
                if commented_this_round >= max_comments_per_round:
                    log.info(f"🛑 Reached max comments per round ({max_comments_per_round}) — stopping early")
                    break

                if is_already_commented(urn):
                    continue

                short_urn = urn[:50]
                log.info(f"👀 Processing post {short_urn}...")

                try:
                    post = page.locator(f'div[data-urn="{urn}"]').first
                    if not await post.count():
                        log.warning(f"⏭️  Post {short_urn} no longer in DOM — skipping")
                        continue

                    post_text = await get_post_text(post)
                    if not post_text:
                        log.info(f"⏭️  Skipping {short_urn} — no usable text found")
                        mark_as_commented(urn)
                        continue

                    log.debug(f"📄 Post text preview: {post_text[:120].replace(chr(10), ' ')}...")

                    if is_content_already_commented(post_text):
                        log.info(f"⏭️  Skipping {short_urn} — identical content already commented (different URN)")
                        mark_as_commented(urn)
                        continue

                    log.info(f"✍️  Generating comment for {short_urn}")
                    comment = await generate_comment(post_text)
                    log.info(f"💬 Comment: \"{comment}\"")

                    mark_as_commented(urn, post_text)
                    log.debug(f"🗄️  Marked URN + content hash in DB")

                    await post.scroll_into_view_if_needed()
                    await page.wait_for_timeout(500)
                    log.debug("🖱️  Scrolled post into view")

                    await post.locator('button[aria-label="Comment"]').first.click(timeout=8000)
                    await page.wait_for_timeout(1500)
                    log.debug("🖱️  Opened comment box")

                    comment_box = post.locator('div[role="textbox"][contenteditable="true"]').first
                    await comment_box.click()
                    await page.wait_for_timeout(500)

                    await comment_box.fill("")
                    type_delay = random.randint(15, 35)
                    log.debug(f"⌨️  Typing comment with {type_delay}ms delay per char")
                    await comment_box.type(comment, delay=type_delay)

                    await comment_box.dispatch_event("input")
                    await comment_box.dispatch_event("change")
                    await page.wait_for_timeout(2000)

                    submitted = await submit_comment(page, comment_box, post)

                    if submitted:
                        commented_this_round += 1
                        log.info(f"✅ Successfully commented on {short_urn} ({commented_this_round}/{max_comments_per_round} this round)")
                    else:
                        log.warning(f"⚠️  Failed to submit comment for {short_urn}")

                    wait_secs = random.uniform(18, 38)
                    log.info(f"⏳ Waiting {wait_secs:.1f}s before next post")
                    await asyncio.sleep(wait_secs)

                except Exception as e:
                    log.error(f"❌ Exception processing post {short_urn}: {e}", exc_info=True)
                    continue

            log.info(f"✅ Round #{round_number} complete — commented on {commented_this_round} posts")

            log.debug("📜 Scrolling feed to load more posts")
            await page.evaluate("window.scrollBy(0, 700)")
            scroll_wait = random.randint(25, 45)
            log.debug(f"⏳ Waiting {scroll_wait}s after scroll")
            await asyncio.sleep(scroll_wait)

            wait_minutes = 30
            log.info(f"😴 Sleeping {wait_minutes} minutes before next round...")
            await asyncio.sleep(wait_minutes * 60)


asyncio.run(run())