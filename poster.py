"""
poster.py — Post queued clips to YouTube Shorts + Instagram Reels on schedule.

Runs on GitHub Actions (see .github/workflows/poster.yml), so no PC has to be on.
The queue is queue.json: Claude adds posts there, this script publishes the ones
that are due, writes the public links back, and the workflow commits the file.

A post in queue.json:
  {"when": "2026-10-12T20:30:00+02:00",            local time with UTC offset
   "drive": "https://drive.google.com/file/d/<id>/view",   shared "anyone with the link"
   "title": "YouTube title", "caption": "Instagram caption",
   "status": "queued"}

After posting it gains "youtube" / "instagram" links and status "posted"; a post
more than MAX_LATE hours overdue becomes "missed" instead of going out late.

Usage:
  python poster.py run [--dry-run]     publish what is due
  python poster.py refresh-ig          extend the Instagram token (monthly workflow)

Secrets come from the environment: YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN, IG_TOKEN.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

QUEUE = Path(__file__).resolve().parent / "queue.json"
LOOKAHEAD = 12     # minutes: a run picks up posts due this soon and waits for the exact time
MAX_LATE = 3       # hours: older than this is marked "missed"
RELEASE = "media"  # GitHub release that briefly hosts the video so Instagram can fetch it
IG = "https://graph.instagram.com"


def call(url: str, data=None, headers=None, method=None):
    """HTTP request; returns (parsed JSON or text, response headers)."""
    if isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return json.loads(body), r.headers
            except ValueError:
                return body, r.headers
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{e.code} from {url.split('?')[0]}: {e.read().decode('utf-8', 'replace')[:500]}")


def download(drive_link: str, dest: Path) -> Path:
    file_id = re.search(r"/d/([\w-]+)|[?&]id=([\w-]+)", drive_link)
    if not file_id:
        raise RuntimeError(f"Not a Drive file link: {drive_link}")
    url = "https://drive.usercontent.google.com/download?export=download&confirm=t&id=" + (file_id[1] or file_id[2])
    with urllib.request.urlopen(url, timeout=600) as r:
        data = r.read()
    if data[4:8] != b"ftyp":
        raise RuntimeError("Drive did not return an MP4 — is the file shared with 'anyone with the link'?")
    dest.write_bytes(data)
    return dest


def post_youtube(video: Path, post: dict, defaults: dict) -> str:
    token, _ = call("https://oauth2.googleapis.com/token", {
        "grant_type": "refresh_token", "client_id": os.environ["YT_CLIENT_ID"],
        "client_secret": os.environ["YT_CLIENT_SECRET"], "refresh_token": os.environ["YT_REFRESH_TOKEN"]})
    auth = {"Authorization": "Bearer " + token["access_token"]}
    meta = {
        "snippet": {"title": post["title"], "description": post.get("description", post["caption"]),
                    "tags": post.get("tags", defaults.get("tags", [])),
                    "categoryId": str(defaults.get("categoryId", 20))},
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    size = video.stat().st_size
    _, headers = call(
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        json.dumps(meta).encode(),
        {**auth, "Content-Type": "application/json; charset=UTF-8",
         "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)})
    done, _ = call(headers["Location"], video.read_bytes(), {**auth, "Content-Type": "video/mp4"}, method="PUT")
    if done.get("status", {}).get("privacyStatus") != "public":
        print("  ! YouTube stored it as", done.get("status", {}).get("privacyStatus"))
    return "https://www.youtube.com/shorts/" + done["id"]


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def post_instagram(video: Path, post: dict) -> str:
    # Instagram only accepts a public URL, so park the file on a release asset while it fetches.
    repo = os.environ["GITHUB_REPOSITORY"]
    if subprocess.run(["gh", "release", "view", RELEASE], capture_output=True).returncode:
        gh("release", "create", RELEASE, "--title", "media", "--notes", "Temporary video hosting for poster.py")
    gh("release", "upload", RELEASE, str(video), "--clobber")
    try:
        token = os.environ["IG_TOKEN"]
        box, _ = call(f"{IG}/me/media", {
            "media_type": "REELS", "caption": post["caption"], "access_token": token,
            "video_url": f"https://github.com/{repo}/releases/download/{RELEASE}/{video.name}"})
        for _ in range(40):
            time.sleep(15)
            state, _ = call(f"{IG}/{box['id']}?fields=status_code,status&access_token={token}")
            if state["status_code"] == "FINISHED":
                break
            if state["status_code"] in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"Instagram rejected the video: {state.get('status')}")
        else:
            raise RuntimeError("Instagram was still processing the video after 10 minutes")
        media, _ = call(f"{IG}/me/media_publish", {"creation_id": box["id"], "access_token": token})
        link, _ = call(f"{IG}/{media['id']}?fields=permalink&access_token={token}")
        return link["permalink"]
    finally:
        subprocess.run(["gh", "release", "delete-asset", RELEASE, video.name, "-y"], capture_output=True)


def run(dry: bool):
    queue = json.loads(QUEUE.read_text(encoding="utf-8"))
    failed = False
    for n, post in enumerate(queue["posts"]):
        if post.get("status") != "queued":
            continue
        when = datetime.fromisoformat(post["when"])
        now = datetime.now(timezone.utc)
        if when > now + timedelta(minutes=LOOKAHEAD):
            continue
        if now - when > timedelta(hours=MAX_LATE):
            post["status"] = "missed"
            print(f"missed  {post['when']}  {post['title']}")
            continue
        print(f"posting {post['when']}  {post['title']}")
        if dry:
            continue
        time.sleep(max(0, (when - datetime.now(timezone.utc)).total_seconds()))
        try:
            video = download(post["drive"], Path(f"clip_{n}.mp4"))
            # each platform is recorded as soon as it succeeds, so a retry never double-posts
            if not post.get("youtube"):
                post["youtube"] = post_youtube(video, post, queue.get("defaults", {}))
                print("  ", post["youtube"])
            if not post.get("instagram"):
                post["instagram"] = post_instagram(video, post)
                print("  ", post["instagram"])
            post["status"] = "posted"
            post["posted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            post.pop("error", None)
        except Exception as e:
            post["error"] = str(e)
            failed = True
            print("  ! failed:", e)
        QUEUE.write_text(json.dumps(queue, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    if not dry:
        QUEUE.write_text(json.dumps(queue, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    # a failed run shows red in GitHub and triggers its notification email
    sys.exit(1 if failed else 0)


def refresh_ig():
    """Extend the 60-day Instagram token and store it back as the IG_TOKEN secret."""
    new, _ = call(f"{IG}/refresh_access_token?grant_type=ig_refresh_token&access_token={os.environ['IG_TOKEN']}")
    subprocess.run(["gh", "secret", "set", "IG_TOKEN"], input=new["access_token"], text=True, check=True)
    print(f"Instagram token extended for {new['expires_in'] // 86400} days")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run", "refresh-ig"])
    ap.add_argument("--dry-run", action="store_true", help="list what would be posted, post nothing")
    args = ap.parse_args()
    refresh_ig() if args.command == "refresh-ig" else run(args.dry_run)
