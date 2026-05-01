import asyncio
import hashlib
import logging
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime
from urllib.parse import quote, unquote
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
FEED_HOME   = "https://www.linkedin.com/feed/"


def _truthy_env(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


LINKEDIN_HEADLESS = _truthy_env(
    "LINKEDIN_HEADLESS", "true"
)  # always headless in production dashboards
LINKEDIN_WAIT_NETWORK_IDLE = _truthy_env(
    "LINKEDIN_WAIT_NETWORK_IDLE",
    "true",
)  # default on: headless feeds often hydrate late

VIEWPORT_W, VIEWPORT_H = 1365, 900
# Recent Chrome UA; stale UAs hurt headless consistency on LinkedIn.
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

DEFAULT_DAILY_LIMIT = 20

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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS comment_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            urn       TEXT,
            post_text TEXT,
            comment   TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    log.info(f"🗄️  Database initialised: {DB_FILE}")

def get_daily_limit() -> int:
    """Read the daily comment limit from DB (live, so UI changes take effect next round)."""
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT value FROM bot_config WHERE key='daily_limit'").fetchone()
        conn.close()
        return int(row["value"]) if row else DEFAULT_DAILY_LIMIT
    except Exception:
        return DEFAULT_DAILY_LIMIT

def get_today_comment_count() -> int:
    """How many comments have already been posted today."""
    try:
        conn = sqlite3.connect(DB_FILE)
        count = conn.execute(
            "SELECT COUNT(*) FROM comment_log WHERE DATE(created_at) = DATE('now')"
        ).fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0

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
                    model="gemini-2.0-flash"
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
FEED_READY_SELECTOR = (
    '[data-urn^="urn:li:activity:"], '
    '[data-activity-urn^="urn:li:activity:"], '
    '[data-urn^="urn:li:ugcPost:"], '
    '[data-activity-urn^="urn:li:ugcPost:"], '
    "div.feed-shared-update-v2, article.feed-shared-update-v2, "
    '[class*="feed-shared-update"], [class*="feed-shared-update-v2"], '
    # Permalink anchors (omit main — SPA / headless often omits semantic wrappers)
    'a[href*="/feed/update/"], a[href*="feed/update"]'
)

URN_TAGS = (
    '[data-urn^="urn:li:activity:"], [data-activity-urn^="urn:li:activity:"], '
    '[data-urn^="urn:li:ugcPost:"], [data-activity-urn^="urn:li:ugcPost:"]'
)

CARD_SELECTOR = (
    'div.feed-shared-update-v2, article.feed-shared-update-v2, '
    '[class*="feed-shared-update-v2"], [class*="feed-shared-update"]'
)

ACTIVITY_PREFIX = ('urn:li:activity:', 'urn:li:ugcPost:')

# Extract LinkedIn URNs from raw href / onclick data (URLs may be %-encoded).
URN_LI_CHUNK = re.compile(r'urn:li:(activity|ugcPost):(\d+)', re.I)
# Rare short paths: linkedin.com/feed/update/7123456789123456890
_FEED_NUM_ID = re.compile(r"/feed/update/(\d{12,24})(?:[/\?]|$)", re.I)
# Vanity share links: linkedin.com/posts/user-text-7390123456789012345
_POSTS_TAIL_ID = re.compile(r"/posts/[^\s\"'<>]+-(\d{12,})(?:\?|[/#\"]|$)", re.I)


def _looks_like_activity_urn(val: str | None) -> bool:
    return bool(val) and val.startswith(ACTIVITY_PREFIX)


def _canon_li_urn(m: re.Match) -> str:
    kind, num = m.group(1), m.group(2)
    if kind.lower().startswith("activity"):
        return f"urn:li:activity:{num}"
    return f"urn:li:ugcPost:{num}"


def _iter_urns_in_string(raw: str) -> list[str]:
    if not raw:
        return []
    try:
        raw = unquote(raw)
    except Exception:
        pass
    urns = [_canon_li_urn(m) for m in URN_LI_CHUNK.finditer(raw)]
    for m in _FEED_NUM_ID.finditer(raw):
        urns.append(f"urn:li:activity:{m.group(1)}")
    for m in _POSTS_TAIL_ID.finditer(raw):
        urns.append(f"urn:li:activity:{m.group(1)}")
    return urns


_LINK_HARVEST_JS = r"""
() => {
  const sel =
    'a[href*="feed/update"], ' +
    'a[href*="/posts/"], ' +
    'a[href*="urn%3Ali%3Aactivity"], ' +
    'a[href*="li%3Aactivity"], ' +
    'a[href*="ugcPost"], ' +
    'a[href*="activity%3A"]';
  const join = [...document.querySelectorAll(sel)].slice(0, 1800);
  const out = [];
  for (const a of join) {
    const href = (a.getAttribute('href') || '').trim();
    if (!href || href.startsWith('#')) continue;
    if (a.closest('nav[aria-label*="Primary"], header.global-nav, .global-nav')) continue;
    if (a.closest('footer')) continue;
    out.push(href);
  }
  return out;
}
"""


_ACTIVITY_URNS_FROM_DOM_JS = r"""
() => {
  function canon(g1, num) {
    return g1.toLowerCase().startsWith("act") ? `urn:li:activity:${num}` : `urn:li:ugcPost:${num}`;
  }
  function pullSlice(slice) {
    const out = [];
    const r1 = /urn:li:(activity|ugcPost):(\d+)/gi;
    const r2 = /urn%3Ali%3A(activity|ugcPost)%3A(\d+)/gi;
    let m;
    while ((m = r1.exec(slice)) !== null) out.push(canon(m[1], m[2]));
    while ((m = r2.exec(slice)) !== null) out.push(canon(m[1], m[2]));
    return out;
  }
  const h = document.documentElement.outerHTML;
  const win = 650000;
  const overlap = 90000;
  const seen = new Set();
  const merged = [];
  for (let i = 0; i < h.length && merged.length < 400; i += win) {
    const slice = h.slice(Math.max(0, i - overlap), Math.min(h.length, i + win + overlap));
    for (const u of pullSlice(slice)) {
      if (!seen.has(u)) {
        seen.add(u);
        merged.push(u);
        if (merged.length >= 400) break;
      }
    }
  }
  return merged;
}
"""


async def activity_urns_from_markup(page) -> list[str]:
    try:
        raw = await page.evaluate(_ACTIVITY_URNS_FROM_DOM_JS)
    except Exception:
        raw = []
    out: list[str] = []
    if not isinstance(raw, list):
        return []
    for u in raw:
        if isinstance(u, str) and _looks_like_activity_urn(u):
            out.append(u)
    return out


async def dismiss_sticky_alerts(page) -> None:
    for sel in (
        'button[aria-label*="Dismiss"]',
        '[data-test-global-alert-dismiss]',
        "button.artdeco-global-alert__dismiss",
    ):
        btn = page.locator(sel).first
        if not await btn.count():
            continue
        try:
            await btn.click(timeout=2000)
            await page.wait_for_timeout(500)
        except Exception:
            continue


async def activity_urns_from_article_roots(page) -> list[str]:
    """Feed cards often mount as <article> even when outer document omits urn: literals."""
    chunks = await page.evaluate(r"""
      () =>
        [...document.querySelectorAll('main article, [role="main"] article, article')]
          .filter((a) => !a.closest('nav[aria-label*="Primary"], footer.global-footer'))
          .slice(0, 56)
          .map((el) => el.outerHTML.slice(0, 220000))
    """)
    seen: dict[str, None] = {}
    ordered: list[str] = []
    if not isinstance(chunks, list):
        return []
    for blob in chunks:
        if not isinstance(blob, str):
            continue
        for u in _iter_urns_in_string(blob):
            if _looks_like_activity_urn(u) and u not in seen:
                seen[u] = None
                ordered.append(u)
    return ordered


async def hydrate_feed_timeline(page, *, passes: int = 12) -> None:
    """Scroll the viewport so virtualization mounts feed cards."""
    await page.keyboard.press("Home")
    await page.wait_for_timeout(400)
    for i in range(passes):
        await page.mouse.wheel(0, 850 + (i % 4) * 120)
        await page.wait_for_timeout(420 + (i % 3) * 80)
    await page.keyboard.press("Home")
    await page.wait_for_timeout(520)


async def activity_urns_from_page_links(page) -> list[str]:
    urls = await page.evaluate(_LINK_HARVEST_JS)

    seen_flag: dict[str, None] = {}
    ordered: list[str] = []
    for h in urls or []:
        if not isinstance(h, str):
            continue
        for u in _iter_urns_in_string(h):
            if _looks_like_activity_urn(u) and u not in seen_flag:
                seen_flag[u] = None
                ordered.append(u)
    return ordered


async def dom_has_encoded_activity_updates(page) -> bool:
    """Fast hint for wait_for_feed_ready (avoid full-document regex every poll)."""
    if await activity_urns_from_page_links(page):
        return True
    return await page.evaluate("""
      () => {
        const h = document.documentElement.outerHTML;
        const head = h.slice(0, Math.min(h.length, 1800000));
        const tail = h.length > 2000000 ? h.slice(-980000) : "";
        const blob = head + tail;
        return (
          /urn:li:(activity|ugcPost):\\d+/i.test(blob) ||
          /urn%3Ali%3A(activity|ugcPost)%3A\\d+/i.test(blob)
        );
      }
    """)


async def _urn_on_element(el) -> str | None:
    for attr in ('data-urn', 'data-activity-urn'):
        v = await el.get_attribute(attr)
        if _looks_like_activity_urn(v):
            return v
    inner = el.locator(URN_TAGS).first
    if await inner.count():
        return await _urn_on_element(inner)
    return None


async def scoped_post_for_urn(page, urn: str):
    """Smallest workable container for selectors (card, article, or permalink link tree)."""
    empty = page.locator("#linkedin-commenter-scope-missing-xxxx").first

    attr_hit = page.locator(f'[data-urn="{urn}"], [data-activity-urn="{urn}"]').first
    if await attr_hit.count():
        card = attr_hit.locator(
            'xpath=ancestor-or-self::*[contains(@class,"feed-shared-update-v2") or '
            'contains(@class,"feed-shared-update")][1]'
        ).first
        if await card.count():
            return card
        art = attr_hit.locator("xpath=ancestor-or-self::article[1]").first
        if await art.count():
            return art
        return attr_hit

    nid = urn.rsplit(":", 1)[-1]
    if not nid.isdigit():
        return empty

    picked = empty
    for scope_sl in ("main", '[role="main"]'):
        root = page.locator(scope_sl).first
        if not await root.count():
            continue
        lk = root.locator(f"a[href*='{nid}']").first
        if await lk.count():
            picked = lk
            break

    if not await picked.count():
        for pat in (
            f'a[href*="/feed/update/"][href*="{nid}"]',
            f'a[href*="feed/update"][href*="{nid}"]',
            f'a[href*="{nid}"]',
        ):
            alt = page.locator(pat).first
            if await alt.count():
                try:
                    if await alt.evaluate(
                        """(el) => !!el.closest('nav[aria-label*="Primary"],footer,header.global-nav,.global-nav')"""
                    ):
                        continue
                except Exception:
                    pass
                picked = alt
                break

    if not await picked.count():
        return empty

    art_up = picked.locator("xpath=ancestor-or-self::article[1]").first
    if await art_up.count():
        return art_up
    wrap = picked.locator(
        'xpath=ancestor-or-self::*[contains(@class,"feed-shared-update") '
        'or contains(@class,"update-components")][1]'
    ).first
    if await wrap.count():
        return wrap
    return picked


def activity_detail_url(urn: str) -> str:
    """Permalink used when feed shell lacks a hydrated card for this activity URN."""
    return f"https://www.linkedin.com/feed/update/{quote(urn, safe='')}/"


async def scoped_post_on_detail_view(page, urn: str):
    """Single-post /feed/update/ layout — narrower DOM tree than homepage feed."""
    empty = page.locator("#linkedin-commenter-scope-missing-xxxx").first
    nid = urn.rsplit(":", 1)[-1]

    attr_hit = page.locator(
        f'[data-urn="{urn}"], [data-activity-urn="{urn}"], '
        f'[data-urn*="activity:{nid}"], [data-activity-urn*="activity:{nid}"], '
        f'[data-urn*="ugcPost:{nid}"], [data-activity-urn*="ugcPost:{nid}"]'
    ).first
    if await attr_hit.count():
        art = attr_hit.locator("xpath=ancestor-or-self::article[1]").first
        if await art.count():
            return art
        wrap = attr_hit.locator(
            'xpath=ancestor-or-self::*[contains(@class,"feed-shared-update")][1]'
        ).first
        if await wrap.count():
            return wrap
        return attr_hit

    with_comment = (
        page.locator("article")
        .filter(
            has=page.locator(
                'button[aria-label="Comment"], '
                'button[aria-label*="Comment"][aria-expanded], '
                'button.comments-comment-box__open-button'
            )
        )
        .first
    )
    if await with_comment.count():
        return with_comment

    fb_wrap = page.locator('[class*="feed-shared-update"]').first
    if await fb_wrap.count():
        return fb_wrap

    for scope in ('main article', '[role="main"] article', "article.relative"):
        a = page.locator(scope).first
        if await a.count():
            return a

    return empty


async def return_to_feed_home(page) -> None:
    try:
        await page.goto(FEED_HOME, wait_until="domcontentloaded", timeout=75000)
        await page.wait_for_timeout(2000)
        try:
            await wait_for_feed_ready(page, timeout_ms=40000)
        except Exception as e:
            log.warning("⚠️  /feed/ nav ok but readiness soft-fail: %s", e)
    except Exception as e:
        log.warning(f"⚠️  RETURN_TO_FEED_NAV {e}")


async def collect_feed_activity_urns(page) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    def append(u: str | None):
        if u and u not in seen and not is_already_commented(u):
            seen.add(u)
            out.append(u)

    await dismiss_sticky_alerts(page)

    if LINKEDIN_WAIT_NETWORK_IDLE:
        try:
            await page.wait_for_load_state("networkidle", timeout=38000)
        except Exception:
            pass

    await hydrate_feed_timeline(page)

    anchors = await activity_urns_from_page_links(page)
    for u in anchors:
        append(u)

    embedded = await activity_urns_from_markup(page)
    for u in embedded:
        append(u)

    from_articles = await activity_urns_from_article_roots(page)
    for u in from_articles:
        append(u)

    cards = await page.locator(CARD_SELECTOR).all()
    iterable: list = cards or await page.locator(URN_TAGS).all()
    for node in iterable:
        append(await _urn_on_element(node))

    ncards = await page.locator(CARD_SELECTOR).count()
    nun = await page.locator(URN_TAGS).count()
    n_article = await page.locator("article").count()
    if not out:
        log.warning(
            "⚠️  Feed harvest empty · <article>≈%s · cards(class)≈%s · data-attrs≈%s · "
            "anchors≈%s · dom-regex≈%s · articles-scan≈%s — "
            "re-export session (linkedin_state.json) or try LINKEDIN_CHROME_CHANNEL=chrome",
            n_article,
            ncards,
            nun,
            len(anchors),
            len(embedded),
            len(from_articles),
        )
    else:
        log.info(
            "🔗 Resolved %s unseen URNs (anchors:%s markup:%s articles-scan:%s · "
            "<article>≈%s · cards(class)≈%s)",
            len(out),
            len(anchors),
            len(embedded),
            len(from_articles),
            n_article,
            ncards,
        )

    return out


async def wait_for_feed_ready(page, timeout_ms: float = 55000):
    deadline = time.monotonic() + timeout_ms / 1000.0
    last_exc: BaseException | None = None
    scroll_n = 0
    while time.monotonic() < deadline:
        slot_ms = (deadline - time.monotonic()) * 1000.0
        if slot_ms < 300:
            break
        slice_ms = min(4500.0, slot_ms)
        try:
            await page.wait_for_selector(FEED_READY_SELECTOR, timeout=slice_ms)
            return
        except Exception as e:
            last_exc = e

        if await dom_has_encoded_activity_updates(page):
            log.info("✅ Feed detected via permalinks (activity URN in anchor href)")
            return

        await page.mouse.wheel(0, 900)
        scroll_n += 1
        await page.wait_for_timeout(450 + (scroll_n % 4) * 150)
        if scroll_n % 6 == 0:
            await page.keyboard.press("End")
            await page.wait_for_timeout(400)

        if await dom_has_encoded_activity_updates(page):
            log.info("✅ Feed detected via permalinks after scroll")
            return

    if last_exc:
        raise last_exc
    raise TimeoutError("Timed out waiting for feed content")


async def get_post_text(post) -> str:
    selectors = [
        '.feed-shared-update-v2__description .break-words',
        '.feed-shared-text .break-words',
        '.feed-shared-update-v2__description span[dir="ltr"]',
        '[class*="feed-shared-update-v2__description"] .break-words',
        '[class*="feed-shared-update-v2__description"] span[dir="ltr"]',
        '[class*="update-components-text"] span[dir="ltr"]',
        '.update-components-text span[dir="ltr"]',
        '.feed-shared-inline-show-more-text span[dir="ltr"]',
        '[class*="feed-shared-inline-show-more-text"] span[dir="ltr"]',
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
    log.info(
        "   Chromium      : %s (trimmed automation fingerprints)",
        "headless" if LINKEDIN_HEADLESS else "headed",
    )
    if LINKEDIN_WAIT_NETWORK_IDLE:
        log.info("   Feed settle   : waits for networkidle after /feed/")
    log.info("=" * 60)

    init_db()

    async with async_playwright() as p:
        _launch = {
            "headless": LINKEDIN_HEADLESS,
            "args": list(_CHROME_ARGS),
            "ignore_default_args": ["--enable-automation"],
        }
        _ch = os.getenv("LINKEDIN_CHROME_CHANNEL", "").strip()
        if _ch:
            _launch["channel"] = _ch
        browser = await p.chromium.launch(**_launch)
        log.info("🌐 Chromium launched")

        _ctx_kw = dict(
            viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
            user_agent=CHROME_USER_AGENT,
            locale="en-US",
            timezone_id=os.getenv("LINKEDIN_TZ", "UTC"),
            permissions=["notifications"],
            java_script_enabled=True,
        )

        if os.path.exists(STATE_FILE):
            context = await browser.new_context(
                storage_state=STATE_FILE,
                **_ctx_kw,
            )
            await context.add_init_script(_ANTIDETECT_INIT)
            log.info("✅ Loaded saved LinkedIn session")
        else:
            log.info("🔑 No state file — performing fresh login")
            context = await browser.new_context(
                **_ctx_kw,
            )
            await context.add_init_script(_ANTIDETECT_INIT)
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
        await page.goto(FEED_HOME, wait_until="domcontentloaded", timeout=90000)
        await dismiss_sticky_alerts(page)
        await page.wait_for_timeout(3500)
        if LINKEDIN_WAIT_NETWORK_IDLE:
            try:
                await page.wait_for_load_state("networkidle", timeout=45000)
            except Exception:
                pass

        try:
            await wait_for_feed_ready(page)
            log.info("✅ Feed loaded")
        except Exception:
            await page.mouse.wheel(0, 900)
            await page.wait_for_timeout(1800)
            try:
                await wait_for_feed_ready(page, timeout_ms=15000)
                log.info("✅ Feed loaded after scroll")
            except Exception:
                try:
                    n_raw = await page.evaluate(
                        """() => document.querySelectorAll('a[href*="feed/update"]').length"""
                    )
                    n_harvest = len(await activity_urns_from_page_links(page))
                except Exception:
                    n_raw = -1
                    n_harvest = -1
                log.error(
                    "❌ Feed markers not found — refresh linkedin_state.json (login in a real browser "
                    "then copy storage) or complete any security checkpoint. See screenshot."
                )
                log.error(f"   diagnostics: generic feed/update anchors={n_raw} · parsed URNs={n_harvest}")
                screenshot_path = f"debug_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                await page.screenshot(path=screenshot_path)
                log.error(f"📸 Screenshot: {screenshot_path}")

        round_number = 0

        while True:
            round_number += 1
            log.info("─" * 50)
            log.info(f"🔄 Round #{round_number} started")

            # ── Daily limit check ──────────────────────────
            daily_limit      = get_daily_limit()
            today_count      = get_today_comment_count()
            remaining_today  = daily_limit - today_count

            log.info(f"📅 Daily limit: {daily_limit} | Today so far: {today_count} | Remaining: {remaining_today}")

            if remaining_today <= 0:
                log.info(f"🛑 Daily limit of {daily_limit} reached — sleeping until next round")
                await asyncio.sleep(30 * 60)
                log.info("🔄 Refreshing feed page...")
                await page.goto(FEED_HOME)
                try:
                    await wait_for_feed_ready(page)
                except Exception:
                    pass
                continue

            # Cap per-round to whichever is smaller: 6 or remaining quota
            max_comments_per_round = min(6, remaining_today)
            # ──────────────────────────────────────────────

            commented_this_round = 0
            all_urns = await collect_feed_activity_urns(page)

            for urn in all_urns:
                if commented_this_round >= max_comments_per_round:
                    log.info(f"🛑 Max comments/round reached ({max_comments_per_round})")
                    break

                # Re-check daily limit live in case it was changed mid-round from UI
                daily_limit     = get_daily_limit()
                today_count     = get_today_comment_count()
                remaining_today = daily_limit - today_count
                if remaining_today <= 0:
                    log.info(f"🛑 Daily limit ({daily_limit}) reached mid-round — stopping")
                    break

                if is_already_commented(urn):
                    continue

                short_urn = urn[:50]
                log.info(f"👀 Processing {short_urn}...")

                opened_detail = False
                try:
                    post = await scoped_post_for_urn(page, urn)
                    if not await post.count():
                        durl = activity_detail_url(urn)
                        log.info(
                            "📎 No hydrated card on feed — opening detail %s…",
                            durl[:88] + ("…" if len(durl) > 88 else ""),
                        )
                        await page.goto(durl, wait_until="domcontentloaded", timeout=75000)
                        await page.wait_for_timeout(2800)
                        opened_detail = True
                        post = await scoped_post_on_detail_view(page, urn)

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
                finally:
                    if opened_detail:
                        await return_to_feed_home(page)

            log.info(f"✅ Round #{round_number} done — {commented_this_round} comments made")
            await page.evaluate("window.scrollBy(0, 700)")
            await asyncio.sleep(random.randint(25, 45))
            log.info("😴 Sleeping 30 minutes before next round...")
            await asyncio.sleep(30 * 60)

            log.info("🔄 Refreshing feed page...")
            await page.goto(FEED_HOME)
            try:
                await wait_for_feed_ready(page)
                log.info("✅ Feed refreshed successfully")
            except Exception:
                log.error("❌ Feed reload failed — will retry next round")


if __name__ == "__main__":
    asyncio.run(run())