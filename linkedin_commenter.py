import asyncio
import hashlib
import random
import sqlite3
import os
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from google import genai
from openai import AsyncOpenAI

load_dotenv()

EMAIL = os.getenv("LINKEDIN_EMAIL")
PASSWORD = os.getenv("LINKEDIN_PASSWORD")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LLM_API_KEY = os.getenv("LLM_API_KEY")
USE_GEMINI = os.getenv("USE_GEMINI", "true").lower() == "true"
STATE_FILE = "linkedin_state.json"
DB_FILE = "commented_posts.db"

groq_client = AsyncOpenAI(api_key=LLM_API_KEY, base_url="https://api.groq.com/openai/v1") if LLM_API_KEY else None

def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("CREATE TABLE IF NOT EXISTS commented (urn TEXT PRIMARY KEY)")
    # NEW: second table to deduplicate by content fingerprint
    conn.execute("CREATE TABLE IF NOT EXISTS commented_hashes (hash TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

def _content_hash(text: str) -> str:
    """MD5 of the first 500 chars — cheap, collision-resistant enough."""
    return hashlib.md5(text.strip()[:500].encode()).hexdigest()

def is_already_commented(urn: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    exists = conn.execute("SELECT 1 FROM commented WHERE urn=?", (urn,)).fetchone()
    conn.close()
    return exists is not None

def is_content_already_commented(text: str) -> bool:
    """Return True if we've already commented on a post with this content."""
    h = _content_hash(text)
    conn = sqlite3.connect(DB_FILE)
    exists = conn.execute("SELECT 1 FROM commented_hashes WHERE hash=?", (h,)).fetchone()
    conn.close()
    return exists is not None

def mark_as_commented(urn: str, text: str = ""):
    """Record both the URN and (optionally) the content hash."""
    conn = sqlite3.connect(DB_FILE)
    conn.execute("INSERT OR IGNORE INTO commented (urn) VALUES (?)", (urn,))
    if text:
        h = _content_hash(text)
        conn.execute("INSERT OR IGNORE INTO commented_hashes (hash) VALUES (?)", (h,))
    conn.commit()
    conn.close()

async def generate_comment(post_text: str) -> str:
    """Generate a comment using LLM (Gemini or Groq)."""
    prompt = "you are a software engineer, you work in backend but can handle a bit of frontend and devops, aspire to be a solution architect. Write a short (1-2 sentences), professional, human-sounding LinkedIn comment. Add value or show genuine interest. No emojis."
    full_prompt = f"{prompt}\n\nPost: {post_text[:700]}"

    if USE_GEMINI:
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=full_prompt
        )
        return response.text.strip()
    elif groq_client:
        resp = await groq_client.chat.completions.create(
            model="llama-3.1-70b-versatile",
            messages=[{"role": "user", "content": full_prompt}],
            max_tokens=80,
            temperature=0.7
        )
        return resp.choices[0].message.content.strip()
    else:
        print("⚠️ No LLM API configured, using mock comments")
        mock_comments = [
            "Great insights! Thanks for sharing this perspective.",
            "This is really valuable information. Appreciate the post!",
            "Interesting point! Looking forward to more content like this.",
            "Well said! This resonates with my experience as well.",
            "Thanks for breaking this down so clearly!",
            "Excellent analysis! This gives me a lot to think about.",
            "Really appreciate you sharing this. Very helpful!",
            "This is spot on! Great work putting this together."
        ]
        index = len(post_text.strip()) % len(mock_comments)
        return mock_comments[index]

async def get_post_text(post) -> str:
    """Extract only the main post body text, not comments or links."""
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
    """Try to submit the comment exactly once. Returns True if submitted."""

    # Scope to the post element to avoid clicking submit on a different open comment box
    submit_btn = post.locator('button.comments-comment-box__submit-button--cr').first
    if not await submit_btn.count():
        submit_btn = post.locator('button[class*="submit-button"]').first
    if not await submit_btn.count():
        submit_btn = page.locator('button.comments-comment-box__submit-button--cr').first

    try:
        await submit_btn.wait_for(state="visible", timeout=5000)

        is_disabled = await submit_btn.get_attribute("disabled")
        print(f"Submit button disabled: {is_disabled}")

        if is_disabled is not None:
            await comment_box.dispatch_event("input")
            await page.wait_for_timeout(1000)
            is_disabled = await submit_btn.get_attribute("disabled")
            if is_disabled is not None:
                print("⚠️ Submit button still disabled, aborting to avoid double comment")
                return False

        await submit_btn.click()
        print("✅ Clicked submit button once")
        await page.wait_for_timeout(4000)
        return True

    except Exception as e:
        print(f"⚠️ Submit button not found: {e}")
        return False

async def run():
    init_db()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, args=["--no-sandbox"])
        #browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])

        if os.path.exists(STATE_FILE):
            context = await browser.new_context(
                storage_state=STATE_FILE,
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            print("✅ Loaded saved LinkedIn session")
        else:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            await page.goto("https://www.linkedin.com/login")
            await page.fill('input[name="session_key"]', EMAIL)
            await page.fill('input[name="session_password"]', PASSWORD)
            await page.click('button[type="submit"]')
            await page.wait_for_timeout(15000)
            await context.storage_state(path=STATE_FILE)
            print("✅ First login done → session saved")

        page = await context.new_page()
        await page.goto("https://www.linkedin.com/feed/")

        try:
            await page.wait_for_selector('div[data-urn^="urn:li:activity:"]', timeout=15000)
            print("✅ Feed loaded")
        except:
            print("⚠️ Feed selector not found — may not be logged in or LinkedIn changed DOM")
            await page.screenshot(path="debug_screenshot.png")
            print("📸 Screenshot saved to debug_screenshot.png")

        while True:
            print("🔄 Scanning feed...")

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

            print(f"Found {len(post_containers)} posts, {len(all_urns)} with unseen URNs")

            for urn in all_urns:
                if commented_this_round >= max_comments_per_round:
                    break

                if is_already_commented(urn):
                    continue

                try:
                    post = page.locator(f'div[data-urn="{urn}"]').first
                    if not await post.count():
                        print(f"⏭️ Post {urn[:40]} no longer in DOM, skipping")
                        continue

                    post_text = await get_post_text(post)
                    if not post_text:
                        print(f"⏭️ Skipping {urn[:40]} — no usable text found")
                        mark_as_commented(urn)  # Don't revisit empty posts
                        continue

                    # ── NEW: skip if we've already commented on identical content ──
                    if is_content_already_commented(post_text):
                        print(f"⏭️ Skipping {urn[:40]} — same content already commented (different URN)")
                        mark_as_commented(urn)  # record URN too so we skip faster next time
                        continue

                    comment = await generate_comment(post_text)
                    print(f"Post URN: {urn[:50]}...\nComment: {comment}")

                    # Mark BEFORE attempting — prevents double-comment on retry
                    # Pass post_text so the content hash is also stored
                    mark_as_commented(urn, post_text)

                    # Scroll post into view before clicking
                    await post.scroll_into_view_if_needed()
                    await page.wait_for_timeout(500)

                    # Open comment box
                    await post.locator('button[aria-label="Comment"]').first.click(timeout=8000)
                    await page.wait_for_timeout(1500)

                    # Focus and type into the comment box — scoped to THIS post, not the whole page
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
                        print(f"✅ Commented on {urn[:50]}")
                        commented_this_round += 1
                    else:
                        print(f"⚠️ Could not submit for {urn[:50]} — skipping")

                    await asyncio.sleep(random.uniform(18, 38))

                except Exception as e:
                    print(f"⚠️ Skipped post {urn[:40]}: {e}")
                    continue

            print(f"✅ Round done — commented on {commented_this_round} posts")

            await page.evaluate("window.scrollBy(0, 700)")
            await asyncio.sleep(random.randint(25, 45))

            print("⏳ Waiting 1 hour before next scan...")
            await asyncio.sleep(1800)
            

asyncio.run(run())