"""
whop.py — Submit freshly posted clips to their Whop Content Rewards campaign.

Runs on GitHub Actions right after poster.py and tiktok.py. Whop only accepts a link within
30 minutes of posting, so this takes every post in queue.json that has a "whop"
campaign path and links without a result yet, and submits each link through the
campaign's "Submit clip" form in a real Chrome window (under xvfb).

The Whop login comes from the WHOP_STATE secret: the browser cookies exported on
the PC with  scripts\\whop_submit.py export-state.

Usage:
  python whop.py run      submit what is pending, write results into queue.json
  python whop.py check    only confirm the Whop login works from here
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

QUEUE = Path(__file__).resolve().parent / "queue.json"
APP_PAGE = "https://whop.com/discover/app/app_QRxsQodZgK1r4D/"   # Content Rewards app
WINDOW = 29        # minutes after posting that Whop still takes the link
SITES = ("youtube", "instagram", "tiktok")


def open_app(page, campaign: str):
    """Load the Content Rewards app (an iframe on apps.whop.com) and go to the campaign."""
    page.goto(APP_PAGE, wait_until="domcontentloaded")
    page.wait_for_selector('iframe[src*="apps.whop.com"]', timeout=45000)
    page.wait_for_timeout(5000)
    app = next(f for f in page.frames if "apps.whop.com" in f.url)
    app.goto(app.url.split("/c/")[0] + campaign, wait_until="domcontentloaded")
    page.wait_for_timeout(5000)
    return app


def submit(page, campaign: str, link: str) -> str:
    """Submit one video link; returns "submitted" or "NOT confirmed: <what Whop showed>"."""
    app = open_app(page, campaign)
    app.get_by_role("button", name="Submit clip").first.click(timeout=30000)
    dialog = app.locator("[role=dialog]")
    dialog.locator('input[type="url"]').fill(link)
    # the real checkbox is hidden behind a styled one, so click its label text
    box = dialog.locator('input[type="checkbox"]')
    dialog.get_by_text("I've read the requirements").click(timeout=10000)
    if not box.is_checked():
        box.evaluate("e => e.click()")
    dialog.get_by_role("button", name="Submit clip").click(timeout=15000)
    # Whop checks the link (linked account, not a duplicate, under 30 minutes), then shows "Clip submitted"
    try:
        dialog.get_by_text("Clip submitted").wait_for(timeout=60000)
        return "submitted"
    except Exception:
        left = dialog.inner_text() if dialog.count() else "the dialog closed without a confirmation"
        return "NOT confirmed: " + " ".join(left.split())[:300]


def browser_page(p):
    """A Chrome window carrying the exported Whop login."""
    browser = p.chromium.launch(channel="chrome", headless=False)
    ctx = browser.new_context(storage_state=json.loads(os.environ["WHOP_STATE"]),
                              viewport={"width": 1280, "height": 900}, locale="en-US")
    return browser, ctx.new_page()


def pending(queue: dict) -> list:
    """[(post, site)] for links that still have to go to Whop and are young enough."""
    now = datetime.now(timezone.utc)
    out = []
    for post in queue["posts"]:
        if not post.get("whop"):
            continue
        posted = datetime.fromisoformat(post.get("posted_at") or post["when"])
        for site in SITES:
            if post.get(site) and site not in post.get("whop_result", {}):
                if now - posted > timedelta(minutes=WINDOW):
                    post.setdefault("whop_result", {})[site] = "too late (over 30 minutes)"
                else:
                    out.append((post, site))
    return out


def save(queue: dict):
    QUEUE.write_text(json.dumps(queue, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def playwright():
    if not os.environ.get("WHOP_STATE", "").strip():
        print("::error::Whop: the WHOP_STATE secret is missing")
        sys.exit(1)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pip = [sys.executable, "-m", "pip", "install", "--quiet", "playwright"]
        if subprocess.run(pip).returncode:   # the runner's system Python refuses plain installs
            subprocess.run(pip + ["--break-system-packages"], check=True)
        # a per-user install lands in a folder this running interpreter has not looked at yet
        import importlib
        import site
        site.addsitedir(site.getusersitepackages())
        importlib.invalidate_caches()
        from playwright.sync_api import sync_playwright
    return sync_playwright()


def run():
    queue = json.loads(QUEUE.read_text(encoding="utf-8"))
    todo = pending(queue)
    save(queue)
    if not todo:
        return
    failed = False
    with playwright() as p:
        browser, page = browser_page(p)
        for post, site in todo:
            try:
                result = submit(page, post["whop"], post[site])
            except Exception as e:
                result = "NOT confirmed: " + str(e).splitlines()[0][:200]
            post.setdefault("whop_result", {})[site] = result
            save(queue)
            if result == "submitted":
                print(f"::notice::Whop: {site} link submitted")
            else:
                failed = True
                print(f"::error::Whop: {site} link {result}. Submit {post[site]} by hand within 30 minutes of posting.")
        browser.close()
    sys.exit(1 if failed else 0)


def check():
    campaign = sys.argv[2] if len(sys.argv) > 2 else next(iter(json.loads(QUEUE.read_text(encoding="utf-8"))["defaults"]["campaigns"].values()))["whop"]
    with playwright() as p:
        browser, page = browser_page(p)
        try:
            app = open_app(page, campaign)
            app.get_by_role("button", name="Submit clip").first.wait_for(timeout=30000)
            signed_out = page.get_by_text("Enter your email").count() or page.get_by_role("button", name="Sign in").count()
            if signed_out:
                raise RuntimeError("the page asks to sign in — the exported login is not accepted here")
            # the Submissions page only lists clips for a signed-in account
            app.goto(app.url.split("/c/")[0] + "/c/submissions", wait_until="domcontentloaded")
            clips = app.locator('button:has-text("Clips")').first
            clips.wait_for(timeout=30000)
            print(f"::notice::Whop login works from GitHub (Submit clip button reached; Submissions shows {' '.join(clips.inner_text().split())})", flush=True)
        except Exception as e:
            text = " ".join(page.inner_text("body").split())[:200] if page.url.startswith("http") else ""
            print(f"::error::Whop check failed: {str(e).splitlines()[0][:200]} | page says: {text}")
            sys.exit(1)
        finally:
            browser.close()


if __name__ == "__main__":
    {"run": run, "check": check}[sys.argv[1] if len(sys.argv) > 1 else "run"]()
