"""
auto_poster.py — LinkedIn auto-poster module
Generates a post (text) using Gemini and an image using Pollinations.ai (free, no API key).
Drop this file next to your existing linkedin_bot.py and import it there.
"""

import asyncio
import json as _json
import logging
import os
import random
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

from google import genai
from google.api_core.exceptions import ResourceExhausted

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Topics — edit freely
# ─────────────────────────────────────────────
POST_TOPICS = [
    "a backend engineering lesson I learned the hard way",
    "why system design interviews miss the point",
    "a practical tip for writing cleaner REST APIs",
    "lessons from debugging a production incident",
    "why every backend dev should understand databases deeply",
    "the difference between a senior and junior engineer mindset",
    "a hot take on microservices vs monoliths",
    "what solution architects actually do day-to-day",
    "how to handle technical debt without burning out",
    "a career tip for engineers who want to grow into leadership",
    "why logging and observability matter more than people think",
    "a quick lesson on caching strategies that saved our system",
    "the underrated skill of writing good documentation",
    "how DevOps culture changes the way you write code",
    "what I wish I knew before my first system design project",
]

DB_FILE        = "commented_posts.db"           # reuse same DB
IMAGE_SAVE_DIR = Path("generated_images")       # local cache of generated images


# ─────────────────────────────────────────────
# DB helpers — track posted days
# ─────────────────────────────────────────────
def init_poster_db():
    IMAGE_SAVE_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS posted_days "
        "(day TEXT PRIMARY KEY, topic TEXT, posted_at TEXT)"
    )
    conn.commit()
    conn.close()
    # Diagnose key availability immediately so there are no silent failures
    key = _pexels_key()
    if key:
        log.info(f"🔑 PEXELS_API_KEY loaded (starts with: {key[:6]}...)")
    else:
        log.error("❌ PEXELS_API_KEY is MISSING — images will be skipped!"
                  " Add it to your .env file. Get a free key at https://www.pexels.com/api")


def already_posted_today() -> bool:
    today = str(date.today())
    conn  = sqlite3.connect(DB_FILE)
    row   = conn.execute(
        "SELECT 1 FROM posted_days WHERE day=?", (today,)
    ).fetchone()
    conn.close()
    return row is not None


def mark_posted_today(topic: str):
    today = str(date.today())
    conn  = sqlite3.connect(DB_FILE)
    conn.execute(
        "INSERT OR IGNORE INTO posted_days (day, topic, posted_at) VALUES (?,?,?)",
        (today, topic, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────
# Choose a topic that hasn't been used recently
# ─────────────────────────────────────────────
def pick_topic() -> str:
    conn = sqlite3.connect(DB_FILE)
    used = {
        row[0]
        for row in conn.execute("SELECT topic FROM posted_days").fetchall()
    }
    conn.close()

    available = [t for t in POST_TOPICS if t not in used]
    if not available:           # all used — reset and cycle again
        log.info("🔄 All topics used — cycling back to full list")
        available = POST_TOPICS

    topic = random.choice(available)
    log.info(f"🎯 Selected topic: '{topic}'")
    return topic


# ─────────────────────────────────────────────
# Generate post TEXT with Gemini
# ─────────────────────────────────────────────
async def generate_post_text(topic: str, gemini_key: str) -> str:
    prompt = f"""You are a software engineer who works in backend, knows a bit of frontend and DevOps,
and aspires to be a solution architect. Write a genuine, engaging LinkedIn post about:

"{topic}"

Rules:
- 150–250 words
- First line must be a hook that stops the scroll
- Use short paragraphs (1-3 sentences each)
- No cringe corporate speak
- No hashtag spam (max 3 relevant hashtags at the end)
- No emojis
- Sound like a real engineer sharing hard-won insight, not a motivational speaker
- End with a question that invites comments

Return ONLY the post text, nothing else."""

    try:
        client   = genai.Client(api_key=gemini_key)
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )
        text = response.text.strip()
        log.info(f"✍️  Post text generated ({len(text)} chars)")
        return text
    except Exception as e:
        log.error(f"❌ Failed to generate post text: {e}")
        raise


# ─────────────────────────────────────────────
# Generate IMAGE — Pexels only
#
# Add to .env:  PEXELS_API_KEY=your_key_here
# Get a free key at: https://www.pexels.com/api  (instant, no credit card)
# ─────────────────────────────────────────────

# Read lazily inside functions — do NOT read at module level because
# load_dotenv() in linkedinposter.py hasn't run yet at import time.
def _pexels_key() -> str:
    return os.getenv("PEXELS_API_KEY", "")

# Maps each topic to the most visually relevant Pexels search query.
# Generic topic phrases ("a lesson", "why every") produce bad results —
# these tighter queries get photos that actually look right on LinkedIn.
TOPIC_SEARCH_MAP: dict[str, str] = {
    "a backend engineering lesson I learned the hard way":        "server room programming code",
    "why system design interviews miss the point":                "whiteboard architecture diagram software",
    "a practical tip for writing cleaner REST APIs":              "API developer laptop code",
    "lessons from debugging a production incident":               "engineer debugging computer screen",
    "why every backend dev should understand databases deeply":   "database server storage technology",
    "the difference between a senior and junior engineer mindset":"software developers team collaboration",
    "a hot take on microservices vs monoliths":                   "software architecture cloud infrastructure",
    "what solution architects actually do day-to-day":            "architect planning technology blueprint",
    "how to handle technical debt without burning out":           "engineer working late stressed laptop",
    "a career tip for engineers who want to grow into leadership":"professional leadership meeting technology",
    "why logging and observability matter more than people think": "monitoring dashboard analytics server",
    "a quick lesson on caching strategies that saved our system": "server performance speed network",
    "the underrated skill of writing good documentation":         "developer writing notes documentation",
    "how DevOps culture changes the way you write code":          "devops pipeline automation cloud",
    "what I wish I knew before my first system design project":   "software engineer planning whiteboard",
}


async def generate_post_image(topic: str) -> Path:
    """
    Fetches a topic-relevant image from Pexels and saves it locally.
    Raises RuntimeError if Pexels fails (caller handles text-only fallback).
    """
    key = _pexels_key()
    if not key:
        raise RuntimeError(
            "PEXELS_API_KEY not set — add it to your .env file. "
            "Get a free key at https://www.pexels.com/api"
        )

    IMAGE_SAVE_DIR.mkdir(exist_ok=True)

    query = TOPIC_SEARCH_MAP.get(topic) or topic   # use mapped query or raw topic
    log.info(f"🖼️  Pexels search: '{query}'")

    data = await asyncio.get_event_loop().run_in_executor(
        None, _fetch_pexels_image, query, key
    )
    save = IMAGE_SAVE_DIR / f"post_{int(time.time())}.jpg"
    save.write_bytes(data)
    log.info(f"✅ Pexels image saved ({len(data)//1024} KB) → {save}")
    return save


def _fetch_pexels_image(query: str, api_key: str) -> bytes:
    """
    Searches Pexels with the given query and downloads the best matching photo.
    Tries landscape orientation first; if no results, retries without orientation filter.
    """
    encoded = urllib.parse.quote(query)

    def _search(orientation_param: str) -> list:
        url = (
            f"https://api.pexels.com/v1/search"
            f"?query={encoded}&per_page=15{orientation_param}"
        )
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": api_key,
                "User-Agent": "Mozilla/5.0 (compatible; LinkedIn-Bot/1.0)",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return _json.loads(resp.read()).get("photos", [])

    # Prefer landscape — looks best on LinkedIn feed
    photos = _search("&orientation=landscape")
    if not photos:
        log.debug("No landscape results — retrying without orientation filter")
        photos = _search("")
    if not photos:
        raise ValueError(f"No Pexels results for: '{query}'")

    # Pick randomly from top 5 for variety across days
    photo   = random.choice(photos[:5])
    img_url = photo["src"]["large2x"]
    log.debug(f"📷 Pexels: '{photo.get('url')}' by {photo['photographer']}")

    req = urllib.request.Request(
        img_url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; LinkedIn-Bot/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = resp.read()

    if len(data) < 4096:
        raise ValueError(f"Downloaded image too small ({len(data)} bytes)")
    return data


# ─────────────────────────────────────────────
# Upload image to LinkedIn (via Playwright)
# Returns the uploaded image URN or None on failure
# ─────────────────────────────────────────────
async def upload_image_via_playwright(page, image_path: Path) -> bool:
    """
    Opens the LinkedIn post composer, attaches the image, types the post,
    and submits. Returns True on success.

    Strategy:
    1. Click the "Start a post" button on the feed
    2. Wait for the composer modal
    3. Click the image/photo attach button
    4. Use set_input_files on the hidden file input
    5. Wait for upload indicator to clear
    6. Type the post text
    7. Click Post
    """
    try:
        log.info("📝 Opening LinkedIn post composer...")

        # Click "Start a post" textarea trigger on the feed
        start_post_btn = page.locator(
            'button[aria-label="Start a post"],'
            ' div[aria-label="Start a post"],'
            ' button.share-box-feed-entry__trigger'
        ).first
        await start_post_btn.wait_for(state="visible", timeout=10000)
        await start_post_btn.click()
        await page.wait_for_timeout(2000)
        log.info("✅ Post composer opened")
        return True   # signal that composer is open; caller types + submits

    except Exception as e:
        log.error(f"❌ Could not open post composer: {e}")
        return False


async def publish_post(page, post_text: str, image_path: Path | None, dry_run: bool = False) -> bool:
    """
    Full flow: open composer → attach image (optional) → type text → submit.
    If dry_run=True, stops before clicking Post so you can inspect.
    Returns True if post was submitted (or dry_run completed) successfully.
    """
    try:
        # 1. Open composer
        log.info("📝 Opening LinkedIn post composer...")
        start_btn = page.locator(
            'button[aria-label="Start a post"],'
            'div.share-box-feed-entry__top-bar button'
        ).first
        await start_btn.wait_for(state="visible", timeout=12000)
        await start_btn.click()
        await page.wait_for_timeout(3000)   # give modal time to fully settle

        # 2. Attach image
        # The "Add a photo" button on the feed toolbar is class="image-detour-btn" —
        # LinkedIn deliberately intercepts it and redirects to a separate flow.
        # The correct button is "Add media" INSIDE the composer modal, which does
        # inject a real file input.
        if image_path and image_path.exists():
            log.info("📎 Attaching image via 'Add media' button inside composer...")

            # Click "Add media" inside the modal via JS
            clicked = await page.evaluate("""() => {
                const all = [...document.querySelectorAll('button')];
                const btn = all.find(b =>
                    (b.getAttribute('aria-label') || '').toLowerCase() === 'add media'
                );
                if (btn) { btn.click(); return btn.outerHTML; }
                return null;
            }""")

            if not clicked:
                log.warning("⚠️  'Add media' button not found — skipping image")
            else:
                log.debug(f"🖱️  'Add media' clicked: {clicked[:80]}")
                await page.wait_for_timeout(2000)

                # LinkedIn now injects a file input — wait for it then set the file
                file_input = page.locator('input[type="file"]').first
                try:
                    await file_input.wait_for(state="attached", timeout=8000)
                    await file_input.set_input_files(str(image_path))
                    log.info("✅ Image attached — waiting for upload to process...")
                    await page.wait_for_timeout(4000)

                    # LinkedIn shows a media edit screen after upload — click Next
                    # to return to the text composer
                    next_btn = await page.evaluate("""() => {
                        const all = [...document.querySelectorAll('button')];
                        const btn = all.find(b => {
                            const t = (b.innerText || '').trim().toLowerCase();
                            const l = (b.getAttribute('aria-label') || '').toLowerCase();
                            return t === 'next' || l === 'next';
                        });
                        if (btn) { btn.click(); return true; }
                        return false;
                    }""")
                    if next_btn:
                        log.info("➡️  Clicked Next — returning to text composer")
                        await page.wait_for_timeout(2000)
                    else:
                        log.debug("ℹ️  No Next button found — already on composer")

                except Exception as e:
                    log.warning(f"⚠️  File input never appeared after 'Add media' click: {e}")

        # 3. Type post text
        log.info("⌨️  Typing post content...")
        text_box = page.locator(
            'div.ql-editor[contenteditable="true"],'
            'div[aria-label="Text editor for creating content"]'
        ).first
        await text_box.wait_for(state="visible", timeout=8000)
        await text_box.click()
        await page.wait_for_timeout(500)
        # Use fill() for the full text — faster and more reliable than type()
        await text_box.fill(post_text)
        await text_box.dispatch_event("input")
        await page.wait_for_timeout(1500)

        # 4. Submit (or stop here if dry run)
        if dry_run:
            log.info("🛑 dry_run=True — stopping here, Post button NOT clicked")
            log.info("👀 Inspect the browser — composer is open with text + image filled in")
            return True

        log.info("🚀 Submitting post...")
        post_btn = page.locator(
            'button.share-actions__primary-action,'
            'button[aria-label="Post"],'
            'button.artdeco-button--primary'
        ).last
        await post_btn.wait_for(state="visible", timeout=8000)

        is_disabled = await post_btn.get_attribute("disabled")
        if is_disabled is not None:
            log.warning("⚠️  Post button disabled — re-dispatching input event")
            await text_box.dispatch_event("input")
            await page.wait_for_timeout(1000)

        await post_btn.click()
        log.info("⏳ Waiting for post confirmation...")
        await page.wait_for_timeout(6000)

        log.info("✅ Post submitted successfully!")
        return True

    except Exception as e:
        log.error(f"❌ Failed to publish post: {e}", exc_info=True)
        # Save debug screenshot
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        await page.screenshot(path=f"debug_post_{ts}.png")
        log.error(f"📸 Debug screenshot saved: debug_post_{ts}.png")
        return False


# ─────────────────────────────────────────────
# Mock post text — used when mock_text=True
# ─────────────────────────────────────────────
MOCK_POST_TEXT = """\
I spent three hours debugging a production issue last week. The root cause? A missing database index.

We had a query that ran fine in staging with 10k rows. In production with 50M rows it was doing a full table scan on every API call. Response times went from 40ms to 8 seconds.

The fix was a single line migration. The investigation took most of the afternoon.

This is why I now treat query performance as a first-class concern during code review, not an afterthought. If your ORM is generating a query, understand what SQL it's actually running. Use EXPLAIN. Check your indexes before you ship.

Staging environments that don't reflect production data size are lying to you. They feel safe but they hide the problems that will wake you up at 2am.

What's the most painful production bug you've tracked down? Was it also hiding in plain sight?

#backend #softwaredevelopment #engineering\
"""


# ─────────────────────────────────────────────
# Main entry — call this from your bot's run()
# ─────────────────────────────────────────────
async def maybe_auto_post(
    page,
    current_gemini_key_fn,
    rotate_gemini_key_fn,
    mock_text: bool = False,
    dry_run: bool = False,
):
    """
    Call once per bot loop iteration.
    Checks if we've already posted today; if not, generates + posts.

    Args:
        page                  : Playwright page (already logged-in LinkedIn feed)
        current_gemini_key_fn : callable → current Gemini API key string
        rotate_gemini_key_fn  : callable → rotate + return next key (or None)
        mock_text             : if True, skip Gemini and use MOCK_POST_TEXT instead
        dry_run               : if True, open composer + type but do NOT click Post
    """
    init_poster_db()

    if not dry_run and already_posted_today():
        log.info("📅 Auto-post: already posted today — skipping")
        return

    log.info("=" * 55)
    log.info("📢 AUTO-POST: Starting daily post generation")
    if mock_text: log.info("   ✏️  mock_text=True  — skipping Gemini")
    if dry_run:   log.info("   🛑  dry_run=True    — Post button will NOT be clicked")
    log.info("=" * 55)

    topic = pick_topic()

    # --- Post text ---
    if mock_text:
        post_text = MOCK_POST_TEXT
        log.info(f"✍️  Using mock post text ({len(post_text)} chars)")
    else:
        post_text = None
        while True:
            key = current_gemini_key_fn()
            if not key:
                log.error("❌ No Gemini key available for post text generation")
                return
            try:
                post_text = await generate_post_text(topic, key)
                break
            except ResourceExhausted:
                log.warning("⚠️  Gemini 429 on text generation — rotating key")
                if rotate_gemini_key_fn() is None:
                    log.error("❌ All keys exhausted — aborting auto-post")
                    return
            except Exception:
                log.error("❌ Unexpected error in text generation — aborting auto-post")
                return

    # --- Generate image via Pollinations.ai (3 retries built-in) ---
    image_path = None
    try:
        image_path = await generate_post_image(topic)
    except Exception:
        log.warning("⚠️  Image generation failed after all retries — posting text-only")

    # --- Publish ---
    success = await publish_post(page, post_text, image_path, dry_run=dry_run)

    if dry_run:
        log.info("🧪 Dry run complete — inspect the browser, nothing was posted")
    elif success:
        mark_posted_today(topic)
        log.info(f"🎉 Auto-post complete! Topic: '{topic}'")
    else:
        log.error("❌ Auto-post failed — will retry tomorrow")

    log.info("=" * 55)