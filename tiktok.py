"""
tiktok.py — Post freshly posted clips to TikTok as well.

Runs on GitHub Actions right after poster.py and before whop.py. TikTok's API
only posts private videos for unaudited apps, so this uploads through TikTok
Studio's web page in a real Chrome window (under xvfb), then writes the public
video link into queue.json as "tiktok", where whop.py picks it up.

The TikTok login comes from the TIKTOK_STATE secret: the browser cookies exported
on the PC with  scripts\\tiktok_upload.py export-state.

A post is skipped when it has "tiktok": false, or when it went out more than
MAX_LATE minutes ago (it would miss the Whop window anyway).

Usage:
  python tiktok.py run      upload what is pending, write links into queue.json
  python tiktok.py check    only confirm the TikTok login works from here
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from whop import playwright, save, QUEUE   # same Playwright install and queue writing

UPLOAD = "https://www.tiktok.com/tiktokstudio/upload?lang=en"
CONTENT = "https://www.tiktok.com/tiktokstudio/content?lang=en"
MAX_LATE = 120     # minutes after the YouTube/Instagram post that TikTok still follows


def dismiss(page):
    """Close the pop-ups TikTok Studio puts over the form (tips, content checks, cookie banner)."""
    for name in ("Got it", "Not now", "Cancel", "Decline optional cookies", "Allow all"):
        btn = page.get_by_role("button", name=name, exact=True)
        if btn.count() and btn.first.is_visible():
            try:
                btn.first.click(timeout=3000)
            except Exception:
                pass


def latest_link(page, before: set) -> str:
    """The public link of the newest video on the account that was not there before."""
    for _ in range(20):
        page.goto(CONTENT, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        hrefs = page.eval_on_selector_all('a[href*="/video/"]', "els => els.map(e => e.href)")
        new = [h for h in hrefs if re.search(r"/video/(\d+)", h) and h.split("?")[0] not in before]
        if new:
            return new[0].split("?")[0]
        page.wait_for_timeout(15000)
    raise RuntimeError("the video did not show up in TikTok Studio's content list")


def video_links(page) -> set:
    page.goto(CONTENT, wait_until="domcontentloaded")
    page.wait_for_timeout(6000)
    return {h.split("?")[0] for h in page.eval_on_selector_all('a[href*="/video/"]', "els => els.map(e => e.href)")}


def upload(page, video: Path, caption: str) -> str:
    """Post one video publicly; returns its tiktok.com link."""
    before = video_links(page)
    page.goto(UPLOAD, wait_until="domcontentloaded")
    if "login" in page.url:
        raise RuntimeError("TikTok asks to sign in — export the login again (TIKTOK_STATE)")
    page.locator('input[type="file"]').first.set_input_files(str(video), timeout=60000)
    # the caption box is a rich-text editor prefilled with the file name
    box = page.locator('[contenteditable="true"]').first
    box.wait_for(timeout=120000)
    dismiss(page)
    box.click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    # typing a "#" opens a hashtag picker; a space after each word closes it again
    for word in caption.split(" "):
        page.keyboard.type(word, delay=15)
        page.wait_for_timeout(400 if word.startswith("#") else 0)
        page.keyboard.press("Space")
    post = page.locator('button[data-e2e="post_video_button"]')
    # Post stays disabled until the upload finishes
    for _ in range(120):
        dismiss(page)
        if post.is_enabled() and page.get_by_text("Uploaded").count():
            break
        page.wait_for_timeout(5000)
    else:
        raise RuntimeError("the upload did not finish within 10 minutes")
    post.click()
    page.wait_for_timeout(3000)
    now = page.get_by_role("button", name="Post now")   # shown when content checks are still running
    if now.count() and now.first.is_visible():
        now.first.click()
    page.wait_for_url(re.compile("tiktokstudio/content"), timeout=180000)
    return latest_link(page, before)


def browser_page(p):
    """A Chrome window carrying the exported TikTok login."""
    browser = p.chromium.launch(channel="chrome", headless=False)
    ctx = browser.new_context(storage_state=json.loads(os.environ["TIKTOK_STATE"]),
                              viewport={"width": 1280, "height": 900}, locale="en-US")
    return browser, ctx.new_page()


def pending(queue: dict) -> list:
    now = datetime.now(timezone.utc)
    out = []
    for post in queue["posts"]:
        if post.get("status") != "posted" or "tiktok" in post or post.get("tiktok_error"):
            continue
        if not post.get("posted_at") or now - datetime.fromisoformat(post["posted_at"]) > timedelta(minutes=MAX_LATE):
            continue
        out.append(post)
    return out


def run():
    if not os.environ.get("TIKTOK_STATE", "").strip():
        print("::warning::TikTok: the TIKTOK_STATE secret is missing, skipping TikTok")
        return
    queue = json.loads(QUEUE.read_text(encoding="utf-8"))
    todo = pending(queue)
    if not todo:
        return
    # the same file poster.py downloaded, if this job still has it
    from poster import download
    failed = False
    with playwright("TIKTOK_STATE") as p:
        browser, page = browser_page(p)
        for post in todo:
            n = queue["posts"].index(post)
            try:
                video = Path(f"clip_{n}.mp4")
                if not video.exists():
                    download(post.get("url") or post["drive"], video)
                post["tiktok"] = upload(page, video.resolve(), post.get("tiktok_caption", post["caption"]))
                print("  ", post["tiktok"])
            except Exception as e:
                post["tiktok_error"] = str(e).splitlines()[0][:300]
                failed = True
                page.screenshot(path=f"tiktok_fail_{n}.png")
                print(f"::error::TikTok upload failed: {post['tiktok_error']}")
            save(queue)
        browser.close()
    sys.exit(1 if failed else 0)


def check():
    with playwright("TIKTOK_STATE") as p:
        browser, page = browser_page(p)
        try:
            page.goto(UPLOAD, wait_until="domcontentloaded")
            page.locator('input[type="file"]').first.wait_for(state="attached", timeout=45000)
            if "login" in page.url:
                raise RuntimeError("the page asks to sign in — the exported login is not accepted here")
            print(f"::notice::TikTok login works from GitHub (upload page reached, {len(video_links(page))} videos listed)")
        except Exception as e:
            page.screenshot(path="tiktok_check.png")
            print(f"::error::TikTok check failed: {str(e).splitlines()[0][:200]} | url: {page.url}")
            sys.exit(1)
        finally:
            browser.close()


if __name__ == "__main__":
    {"run": run, "check": check}[sys.argv[1] if len(sys.argv) > 1 else "run"]()
