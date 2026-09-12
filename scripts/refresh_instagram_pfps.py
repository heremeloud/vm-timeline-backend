#!/usr/bin/env python3
"""Refresh Instagram profile photos into local author uploads.

Run from vm-timeline-backend:
    python3 scripts/refresh_instagram_pfps.py --dry-run
    python3 scripts/refresh_instagram_pfps.py
    python3 scripts/refresh_instagram_pfps.py --redownload-all
    python3 scripts/refresh_instagram_pfps.py --author-id 10 --force

Instagram now requires a logged-in session to look up profile photos - both the
web_profile_info API and the profile page's embedded metadata return nothing useful
to a logged-out request. Supply your own Instagram session by copying the "Cookie"
request header from a logged-in browser tab (DevTools -> Network -> any
instagram.com request -> Request Headers -> Cookie), then either:

    add IG_COOKIE="sessionid=...; csrftoken=...; ds_user_id=...; ..." to vm-timeline-backend/.env

or export it / pass --cookie-file - see load_instagram_cookie() below for the lookup order.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import ssl
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import certifi
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
os.chdir(BACKEND_DIR)
load_dotenv(BACKEND_DIR / ".env")


DB_PATH = BACKEND_DIR / "vm-social.db"
UPLOAD_DIR = BACKEND_DIR / "uploads" / "authors"
PUBLIC_PREFIX = "/static/authors"
DEFAULT_TIMEOUT = 20
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

API_HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "application/json",
    "X-IG-App-ID": "936619743392459",
    "X-ASBD-ID": "129477",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.instagram.com/",
}

IMAGE_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Referer": "https://www.instagram.com/",
}


@dataclass
class AuthorRow:
    id: int
    name: str
    instagram_url: str | None
    ig_pfp_url: str | None


def load_instagram_cookie(cookie_file: str | None) -> str | None:
    """Load the user's own logged-in Instagram session (never auto-fetched)."""
    if cookie_file:
        return Path(cookie_file).expanduser().read_text(encoding="utf-8").strip() or None
    return os.environ.get("IG_COOKIE", "").strip() or None


def csrf_token_from_cookie(cookie: str) -> str | None:
    match = re.search(r"(?:^|;\s*)csrftoken=([^;]+)", cookie)
    return match.group(1) if match else None


def build_api_headers(cookie: str | None) -> dict[str, str]:
    headers = dict(API_HEADERS_BASE)
    if cookie:
        headers["Cookie"] = cookie
        csrf = csrf_token_from_cookie(cookie)
        if csrf:
            headers["X-CSRFToken"] = csrf
    return headers


def build_page_headers(cookie: str | None) -> dict[str, str]:
    headers = dict(HEADERS)
    if cookie:
        headers["Cookie"] = cookie
    return headers


def request_bytes(url: str, headers: dict[str, str], timeout: int) -> tuple[bytes, str]:
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout, context=SSL_CONTEXT) as response:
        content_type = response.headers.get("Content-Type", "")
        return response.read(), content_type


def decode_jsonish_string(value: str) -> str:
    value = html.unescape(value)
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value.replace("\\/", "/")


def normalize_instagram_username(instagram_url: str) -> str | None:
    text = (instagram_url or "").strip()
    if not text:
        return None

    if text.startswith("@"):
        return text[1:].split("/")[0] or None

    if not re.match(r"^https?://", text):
        text = f"https://instagram.com/{text.lstrip('/')}"

    parsed = urlparse(text)
    if "instagram.com" not in parsed.netloc.lower():
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None

    reserved = {"p", "reel", "reels", "stories", "explore", "accounts"}
    if parts[0].lower() in reserved:
        return None

    return parts[0]


def extract_profile_photo_url(page_html: str, username: str) -> str | None:
    # The HTML fallback has no structured way to confirm whose page this is, and
    # Instagram now often serves a generic/login-wall/"not found" shell instead of a
    # real profile page to non-browser requests. Trusting *any* image found on such a
    # page silently saves the wrong photo (this is what corrupted every author's PFP
    # earlier). So: refuse to extract anything unless the page text actually names
    # this username somewhere.
    if not re.search(rf'"username"\s*:\s*"{re.escape(username)}"', page_html, re.IGNORECASE) \
            and f"instagram.com/{username.lower()}" not in page_html.lower():
        return None

    patterns = [
        r'"profile_pic_url_hd"\s*:\s*"([^"]+)"',
        r'"profile_pic_url"\s*:\s*"([^"]+)"',
        r'"profile_picture_url"\s*:\s*"([^"]+)"',
        r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
        r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:image["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, page_html)
        if match:
            return decode_jsonish_string(match.group(1))
    return None


class InstagramRateLimitError(LookupError):
    """Stop the batch when Instagram asks us to slow down."""


def fetch_profile_photo_url(username: str, timeout: int, cookie: str | None) -> str | None:
    api_url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}"
    diagnostics = []
    try:
        body, _ = request_bytes(api_url, build_api_headers(cookie), timeout)
        data = json.loads(body.decode("utf-8", errors="replace"))
        payload = data.get("data") if isinstance(data, dict) else None
        user = (payload.get("user") if isinstance(payload, dict) else None) or {}
        if not isinstance(user, dict):
            user = {}
        # Guard against ever again saving a photo for the wrong account, in case the
        # API misbehaves: only trust a result that actually says it's this username.
        if (user.get("username") or "").lower() != username.lower():
            user = {}
        found = user.get("profile_pic_url_hd") or user.get("profile_pic_url")
        if found:
            return found
        diagnostics.append("API returned no matching profile photo (session may be expired or challenged)")
    except HTTPError as exc:
        if exc.code == 429:
            raise InstagramRateLimitError("API HTTP 429: rate limited; stop and retry later") from exc
        reason = {401: "session rejected", 403: "access denied or session challenged", 429: "rate limited"}.get(exc.code, "request rejected")
        diagnostics.append(f"API HTTP {exc.code}: {reason}")
    except json.JSONDecodeError:
        diagnostics.append("API returned non-JSON content (possibly a login page)")
    except (URLError, TimeoutError, OSError) as exc:
        diagnostics.append(f"API network error: {type(exc).__name__}")

    profile_url = f"https://www.instagram.com/{username}/"
    try:
        body, _ = request_bytes(profile_url, build_page_headers(cookie), timeout)
    except HTTPError as exc:
        if exc.code == 429:
            raise InstagramRateLimitError("Profile page HTTP 429: rate limited; stop and retry later") from exc
        raise LookupError("; ".join(diagnostics + [f"profile page HTTP {exc.code}"])) from exc
    photo = extract_profile_photo_url(body.decode("utf-8", errors="replace"), username)
    if photo:
        return photo
    diagnostics.append("profile page contained no verified photo")
    raise LookupError("; ".join(diagnostics))


def extension_for(content_type: str, url: str) -> str:
    content_type = content_type.split(";")[0].strip().lower()
    by_type = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    if content_type in by_type:
        return by_type[content_type]

    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".jpg"


def should_refresh(author: AuthorRow, force: bool) -> bool:
    current = (author.ig_pfp_url or "").strip()
    if force:
        return True
    if not current:
        return True
    if current.startswith(PUBLIC_PREFIX):
        return False
    return current.startswith("http://") or current.startswith("https://")


def select_authors(conn: sqlite3.Connection, author_id: int | None, name: str | None) -> list[AuthorRow]:
    query = """
        SELECT id, name, instagram_url, ig_pfp_url
        FROM author
        WHERE instagram_url IS NOT NULL
          AND trim(instagram_url) != ''
    """
    params: list[object] = []
    if author_id is not None:
        query += " AND id = ?"
        params.append(author_id)
    query += " ORDER BY id"

    rows = [
        AuthorRow(
            id=row["id"],
            name=row["name"],
            instagram_url=row["instagram_url"],
            ig_pfp_url=row["ig_pfp_url"],
        )
        for row in conn.execute(query, params).fetchall()
    ]
    if name:
        needle = name.lower()
        rows = [row for row in rows if needle in (row.name or "").lower()]
    return rows


def refresh_author(
    conn: sqlite3.Connection,
    author: AuthorRow,
    dry_run: bool,
    force: bool,
    timeout: int,
    cookie: str | None,
) -> tuple[bool, str]:
    username = normalize_instagram_username(author.instagram_url or "")
    if not username:
        return False, "skip: invalid instagram_url"

    if not should_refresh(author, force):
        return False, f"skip: already local ({author.ig_pfp_url})"

    profile_photo_url = fetch_profile_photo_url(username, timeout, cookie)
    if not profile_photo_url:
        return False, "failed: could not find profile photo URL in Instagram page"

    if dry_run:
        return True, f"dry-run: found {profile_photo_url}"

    image_bytes, content_type = request_bytes(profile_photo_url, IMAGE_HEADERS, timeout)
    if not content_type.lower().startswith("image/"):
        return False, f"failed: profile URL did not return an image ({content_type or 'unknown content type'})"

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ext = extension_for(content_type, profile_photo_url)
    filename = f"ig-{author.id}{ext}"
    path = UPLOAD_DIR / filename
    path.write_bytes(image_bytes)

    local_url = f"{PUBLIC_PREFIX}/{filename}"
    conn.execute("UPDATE author SET ig_pfp_url = ? WHERE id = ?", (local_url, author.id))
    author.ig_pfp_url = local_url
    return True, f"updated: {local_url}"


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download current Instagram profile photos and save stable local author URLs."
    )
    parser.add_argument("--dry-run", action="store_true", help="Fetch profile pages but do not download images or update the DB.")
    parser.add_argument(
        "--force",
        "--redownload-all",
        dest="force",
        action="store_true",
        help="Redownload every matching Instagram PFP, including authors that already have a local image.",
    )
    parser.add_argument("--author-id", type=int, help="Refresh one author by ID.")
    parser.add_argument("--name", help="Refresh authors whose name contains this text.")
    parser.add_argument("--limit", type=positive_int, help="Stop after checking this many matching authors.")
    parser.add_argument("--timeout", type=positive_int, default=DEFAULT_TIMEOUT, help=f"HTTP timeout in seconds. Default: {DEFAULT_TIMEOUT}.")
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Seconds to wait between authors, to avoid Instagram rate-limiting your session. Default: 2.0.",
    )
    parser.add_argument(
        "--cookie-file",
        help="Path to a file containing your logged-in Instagram 'Cookie' header. "
             "Falls back to the IG_COOKIE env var. Required now that Instagram blocks logged-out lookups.",
    )
    return parser


def limited(rows: Iterable[AuthorRow], limit: int | None) -> Iterable[AuthorRow]:
    if limit is None:
        yield from rows
        return
    count = 0
    for row in rows:
        if count >= limit:
            return
        yield row
        count += 1


def main() -> int:
    args = build_parser().parse_args()

    cookie = load_instagram_cookie(args.cookie_file)
    if not cookie:
        print(
            "Warning: no Instagram session cookie found (IG_COOKIE env var or --cookie-file). "
            "Instagram now requires a logged-in session for both photo lookup methods, "
            "so every author will likely fail. See the script's module docstring for how to get one.",
            file=sys.stderr,
        )

    checked = 0
    changed = 0
    failures = 0

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        authors = select_authors(conn, args.author_id, args.name)
        rows = list(limited(authors, args.limit))
        for index, author in enumerate(rows):
            checked += 1
            label = f"#{author.id} {author.name}"
            try:
                ok, message = refresh_author(conn, author, args.dry_run, args.force, args.timeout, cookie)
            except InstagramRateLimitError as exc:
                failures += 1
                print(f"{label}: failed: {exc}")
                print("Stopped early to avoid more rate-limited requests. Existing photos are unchanged for this author.")
                break
            except HTTPError as exc:
                ok = False
                message = f"failed: rate limited by Instagram (429) - wait a while or increase --delay" \
                    if exc.code == 429 else f"failed: {exc}"
            except (URLError, TimeoutError, OSError, LookupError) as exc:
                ok = False
                message = f"failed: {exc}"

            if ok:
                changed += 1
            elif message.startswith("failed:"):
                failures += 1

            print(f"{label}: {message}")

            if args.delay and index < len(rows) - 1:
                time.sleep(args.delay)

        if changed and not args.dry_run:
            conn.commit()

    print(f"Done. checked={checked} refreshed={changed} failures={failures} dry_run={args.dry_run}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
