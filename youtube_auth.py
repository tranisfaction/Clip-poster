"""
youtube_auth.py — One-time sign-in that prints the YouTube refresh token for poster.py.

Needs an OAuth client of type "Desktop app" from Google Cloud (YouTube Data API v3
enabled, consent screen published to "In production" — in "Testing" the token dies
after 7 days).

Usage:
  python youtube_auth.py <client id> <client secret>
"""
import http.server
import json
import sys
import urllib.parse
import urllib.request
import webbrowser

PORT = 8765
REDIRECT = f"http://127.0.0.1:{PORT}"
SCOPE = "https://www.googleapis.com/auth/youtube.upload"


class Handler(http.server.BaseHTTPRequestHandler):
    code = None

    def do_GET(self):
        Handler.code = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("code", [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Done. You can close this tab.")

    def log_message(self, *args):
        pass


def main():
    client_id, client_secret = sys.argv[1:3]
    webbrowser.open("https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent"}))
    print("Sign in with the Google account that owns the YouTube channel...")
    with http.server.HTTPServer(("127.0.0.1", PORT), Handler) as server:
        while Handler.code is None:
            server.handle_request()
    data = urllib.parse.urlencode({
        "code": Handler.code, "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": REDIRECT, "grant_type": "authorization_code"}).encode()
    with urllib.request.urlopen("https://oauth2.googleapis.com/token", data) as r:
        print("\nYT_REFRESH_TOKEN =", json.load(r)["refresh_token"])


if __name__ == "__main__":
    main()
