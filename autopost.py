"""
auto_poster.py — LinkedIn auto-poster module
Generates a post (text) using Z.ai (GLM) and an image using Pollinations.ai (free, no API key).
Drop this file next to your existing linkedin_bot.py and import it there.
"""

import asyncio
import logging
import os
import random
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path

from openai import AsyncOpenAI

from zai_llm import generate_text

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
    log.info("🗄️  Poster DB initialised")


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
# Generate post TEXT with Z.ai
# ─────────────────────────────────────────────
async def generate_post_text(topic: str) -> str:
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

    return await generate_text(prompt, max_tokens=1024, temperature=0.8)


# ─────────────────────────────────────────────
# Generate IMAGE — local word cloud
#
# No API key, no internet. Generates in <1 second locally.
# Requires:  pip install wordcloud pillow
# ─────────────────────────────────────────────

# Keywords per topic — repeating a word gives it more weight (= bigger in cloud)
TOPIC_KEYWORDS: dict[str, list[str]] = {
    "a backend engineering lesson I learned the hard way": [
        "backend", "backend", "backend", "engineering", "engineering",
        "production", "lesson", "server", "bug", "debug", "API",
        "architecture", "database", "performance", "latency", "deployment",
        "failure", "learning", "experience", "systems", "code",
    ],
    "why system design interviews miss the point": [
        "system design", "system design", "interview", "interview",
        "architecture", "scalability", "trade-offs", "distributed",
        "real world", "whiteboard", "CAP theorem", "load balancer",
        "cache", "database", "microservices", "communication", "hiring",
    ],
    "a practical tip for writing cleaner REST APIs": [
        "REST", "REST", "API", "API", "API", "clean code", "HTTP",
        "endpoints", "versioning", "JSON", "status codes", "naming",
        "documentation", "Swagger", "OpenAPI", "idempotent", "CRUD",
        "contract", "consistency", "developer experience",
    ],
    "lessons from debugging a production incident": [
        "debugging", "debugging", "production", "production", "incident",
        "logs", "monitoring", "alert", "root cause", "trace", "fix",
        "hotfix", "postmortem", "on-call", "metrics", "downtime",
        "rollback", "investigation", "learning", "resilience",
    ],
    "why every backend dev should understand databases deeply": [
        "database", "database", "database", "SQL", "index", "index",
        "query", "query", "performance", "PostgreSQL", "transactions",
        "ACID", "schema", "normalization", "ORM", "joins", "EXPLAIN",
        "optimization", "storage", "backend", "backend",
    ],
    "the difference between a senior and junior engineer mindset": [
        "senior", "senior", "junior", "junior", "mindset", "mindset",
        "growth", "ownership", "trade-offs", "communication", "code review",
        "mentorship", "experience", "problem solving", "abstraction",
        "pragmatism", "impact", "leadership", "engineering", "clarity",
    ],
    "a hot take on microservices vs monoliths": [
        "microservices", "microservices", "monolith", "monolith",
        "architecture", "architecture", "scalability", "complexity",
        "deployment", "distributed", "service mesh", "Docker", "Kubernetes",
        "trade-offs", "team size", "domain", "coupling", "latency",
        "simple", "pragmatic",
    ],
    "what solution architects actually do day-to-day": [
        "solution architect", "solution architect", "architecture",
        "architecture", "design", "design", "stakeholders", "trade-offs",
        "cloud", "AWS", "Azure", "GCP", "diagrams", "requirements",
        "communication", "strategy", "integration", "patterns", "review",
        "documentation",
    ],
    "how to handle technical debt without burning out": [
        "technical debt", "technical debt", "refactoring", "refactoring",
        "burnout", "sustainability", "prioritization", "code quality",
        "legacy", "engineering", "balance", "planning", "velocity",
        "team health", "shortcuts", "long term", "trade-offs", "culture",
        "pragmatism", "backlog",
    ],
    "a career tip for engineers who want to grow into leadership": [
        "leadership", "leadership", "career", "career", "growth", "growth",
        "communication", "communication", "mentorship", "influence",
        "engineering", "soft skills", "team", "impact", "vision",
        "ownership", "strategy", "promotion", "trust", "collaboration",
    ],
    "why logging and observability matter more than people think": [
        "logging", "logging", "logging", "observability", "observability",
        "monitoring", "monitoring", "metrics", "traces", "alerts",
        "production", "debugging", "OpenTelemetry", "Grafana", "Datadog",
        "incidents", "visibility", "systems", "SRE", "reliability",
    ],
    "a quick lesson on caching strategies that saved our system": [
        "caching", "caching", "caching", "Redis", "Redis", "CDN",
        "performance", "performance", "latency", "TTL", "invalidation",
        "cache miss", "cache hit", "strategy", "distributed cache",
        "Memcached", "scalability", "read-through", "write-through", "speed",
    ],
    "the underrated skill of writing good documentation": [
        "documentation", "documentation", "documentation", "writing",
        "writing", "clarity", "README", "API docs", "Confluence", "Notion",
        "knowledge", "onboarding", "team", "communication", "maintenance",
        "underrated", "developer experience", "code comments", "diagrams",
        "engineering culture",
    ],
    "how DevOps culture changes the way you write code": [
        "DevOps", "DevOps", "DevOps", "CI/CD", "CI/CD", "automation",
        "automation", "culture", "culture", "collaboration", "Docker",
        "Kubernetes", "pipeline", "deployment", "infrastructure",
        "monitoring", "reliability", "shift left", "testing", "agile",
    ],
    "what I wish I knew before my first system design project": [
        "system design", "system design", "architecture", "architecture",
        "lessons", "mistakes", "scalability", "database", "trade-offs",
        "requirements", "scope", "simplicity", "over-engineering",
        "communication", "documentation", "team", "planning", "experience",
        "backend", "learning",
    ],
}

WC_WIDTH  = 1200
WC_HEIGHT = 628    # LinkedIn landscape ratio


def _wc_color_func(word, font_size, position, orientation, random_state=None, **kwargs):
    """Blue/teal/white palette — professional look on dark background."""
    import random as _r
    return _r.choice([
        "#58a6ff", "#79c0ff", "#56d364",
        "#3fb950", "#e3b341", "#ffa657",
        "#f0f6fc", "#8b949e",
    ])


def _generate_wordcloud_sync(topic: str) -> Path:
    from wordcloud import WordCloud

    IMAGE_SAVE_DIR.mkdir(exist_ok=True)
    keywords = TOPIC_KEYWORDS.get(topic, topic.split())
    text     = " ".join(keywords)

    wc = WordCloud(
        width             = WC_WIDTH,
        height            = WC_HEIGHT,
        background_color  = "#0a0a0a",
        max_words         = 60,
        color_func        = _wc_color_func,
        prefer_horizontal = 0.85,
        collocations      = False,
        margin            = 14,
    ).generate(text)

    save = IMAGE_SAVE_DIR / f"wc_{int(time.time())}.png"
    wc.to_file(str(save))
    log.info(f"☁️  Word cloud saved ({save.stat().st_size // 1024} KB) → {save}")
    return save


async def generate_post_image(topic: str) -> Path:
    """Generates a word cloud PNG for the topic. Fast, local, no API needed."""
    return await asyncio.get_event_loop().run_in_executor(
        None, _generate_wordcloud_sync, topic
    )


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
    current_zai_key_fn,
    rotate_zai_key_fn,
    mock_text: bool = False,
    dry_run: bool = False,
):
    """
    Call once per bot loop iteration.
    Checks if we've already posted today; if not, generates + posts.

    Args:
        page                  : Playwright page (already logged-in LinkedIn feed)
        current_zai_key_fn    : callable → current Z.ai API key string
        rotate_zai_key_fn     : callable → rotate + return next key (or None)
        mock_text             : if True, skip Z.ai and use MOCK_POST_TEXT instead
        dry_run               : if True, open composer + type but do NOT click Post
    """
    init_poster_db()

    if not dry_run and already_posted_today():
        log.info("📅 Auto-post: already posted today — skipping")
        return

    log.info("=" * 55)
    log.info("📢 AUTO-POST: Starting daily post generation")
    if mock_text: log.info("   ✏️  mock_text=True  — skipping Z.ai")
    if dry_run:   log.info("   🛑  dry_run=True    — Post button will NOT be clicked")
    log.info("=" * 55)

    topic = pick_topic()

    # --- Post text ---
    if mock_text:
        post_text = MOCK_POST_TEXT
        log.info(f"✍️  Using mock post text ({len(post_text)} chars)")
    else:
        if not current_zai_key_fn():
            log.error("❌ No Z.ai key available for post text generation")
            return
        try:
            post_text = await generate_post_text(topic)
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