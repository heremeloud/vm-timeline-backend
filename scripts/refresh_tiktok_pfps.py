#!/usr/bin/env python3
"""Refresh TikTok profile photos into local author uploads.

Run from vm-timeline-backend:
    python3 scripts/refresh_tiktok_pfps.py --dry-run
    python3 scripts/refresh_tiktok_pfps.py
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    import certifi
except ImportError:
    certifi = None

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
os.chdir(BACKEND_DIR)

DB_PATH = BACKEND_DIR / "vm-social.db"
UPLOAD_DIR = BACKEND_DIR / "uploads" / "authors"
PUBLIC_PREFIX = "/static/authors"
DEFAULT_TIMEOUT = 20
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where() if certifi else None)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

IMAGE_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Referer": "https://www.tiktok.com/",
}


@dataclass
class AuthorRow:
    id: int
    name: str
    tiktok_url: str | None
    tiktok_pfp_url: str | None


def request_bytes(url: str, headers: dict[str, str], timeout: int) -> tuple[bytes, str]:
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout, context=SSL_CONTEXT) as response:
        return response.read(), response.headers.get("Content-Type", "")


def decode_jsonish_string(value: str) -> str:
    value = html.unescape(value)
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value.replace("\\/", "/").replace("\\u002F", "/")


def normalize_tiktok_username(tiktok_url: str) -> str | None:
    text = (tiktok_url or "").strip()
    if not text:
        return None
    if text.startswith("@"):
        return text[1:].split("/")[0] or None
    if not re.match(r"^https?://", text):
        text = f"https://www.tiktok.com/{text.lstrip('/')}"

    parsed = urlparse(text)
    if "tiktok.com" not in parsed.netloc.lower():
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if not parts or not parts[0].startswith("@"):
        return None
    return parts[0][1:] or None


def extract_profile_photo_url(page_html: str) -> str | None:
    patterns = [
        r'"avatarLarger"\s*:\s*"([^"]+)"',
        r'"avatarMedium"\s*:\s*"([^"]+)"',
        r'"avatarThumb"\s*:\s*"([^"]+)"',
        r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
        r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:image["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, page_html)
        if match:
            return decode_jsonish_string(match.group(1))
    return None


def fetch_profile_photo_url(username: str, timeout: int) -> str | None:
    profile_url = f"https://www.tiktok.com/@{username}"
    body, _ = request_bytes(profile_url, HEADERS, timeout)
    return extract_profile_photo_url(body.decode("utf-8", errors="replace"))


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
    current = (author.tiktok_pfp_url or "").strip()
    if force or not current:
        return True
    if current.startswith(PUBLIC_PREFIX):
        return False
    return current.startswith("http://") or current.startswith("https://")


def select_authors(conn: sqlite3.Connection, author_id: int | None, name: str | None) -> list[AuthorRow]:
    query = """
        SELECT id, name, tiktok_url, tiktok_pfp_url
        FROM author
        WHERE tiktok_url IS NOT NULL
          AND trim(tiktok_url) != ''
    """
    params: list[object] = []
    if author_id is not None:
        query += " AND id = ?"
        params.append(author_id)
    query += " ORDER BY sort_order, id"

    rows = [AuthorRow(**dict(row)) for row in conn.execute(query, params).fetchall()]
    if name:
        needle = name.lower()
        rows = [row for row in rows if needle in row.name.lower()]
    return rows


def refresh_author(
    conn: sqlite3.Connection,
    author: AuthorRow,
    dry_run: bool,
    force: bool,
    timeout: int,
) -> tuple[bool, str]:
    username = normalize_tiktok_username(author.tiktok_url or "")
    if not username:
        return False, "skip: invalid tiktok_url"
    if not should_refresh(author, force):
        return False, f"skip: already local ({author.tiktok_pfp_url})"

    profile_photo_url = fetch_profile_photo_url(username, timeout)
    if not profile_photo_url:
        return False, "failed: could not find profile photo URL in TikTok page"
    if dry_run:
        return True, f"dry-run: found {profile_photo_url}"

    image_bytes, content_type = request_bytes(profile_photo_url, IMAGE_HEADERS, timeout)
    if not content_type.lower().startswith("image/"):
        return False, f"failed: profile URL did not return an image ({content_type or 'unknown content type'})"

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ext = extension_for(content_type, profile_photo_url)
    filename = f"tt-{author.id}{ext}"
    (UPLOAD_DIR / filename).write_bytes(image_bytes)

    local_url = f"{PUBLIC_PREFIX}/{filename}"
    conn.execute("UPDATE author SET tiktok_pfp_url = ? WHERE id = ?", (local_url, author.id))
    author.tiktok_pfp_url = local_url
    return True, f"updated: {local_url}"


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download current TikTok profile photos and save stable local author URLs."
    )
    parser.add_argument("--dry-run", action="store_true", help="Fetch profile pages but do not download images or update the DB.")
    parser.add_argument("--force", action="store_true", help="Refresh even when tiktok_pfp_url already points to a local /static/authors file.")
    parser.add_argument("--author-id", type=int, help="Refresh one author by ID.")
    parser.add_argument("--name", help="Refresh authors whose name contains this text.")
    parser.add_argument("--limit", type=positive_int, help="Stop after checking this many matching authors.")
    parser.add_argument("--timeout", type=positive_int, default=DEFAULT_TIMEOUT, help=f"HTTP timeout in seconds. Default: {DEFAULT_TIMEOUT}.")
    return parser


def limited(rows: Iterable[AuthorRow], limit: int | None) -> Iterable[AuthorRow]:
    yield from rows if limit is None else rows[:limit]


def main() -> int:
    args = build_parser().parse_args()
    checked = refreshed = failures = 0

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        for author in limited(select_authors(conn, args.author_id, args.name), args.limit):
            checked += 1
            try:
                ok, message = refresh_author(conn, author, args.dry_run, args.force, args.timeout)
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                ok, message = False, f"failed: {exc}"
            if ok:
                refreshed += 1
            elif message.startswith("failed:"):
                failures += 1
            print(f"#{author.id} {author.name}: {message}")

        if refreshed and not args.dry_run:
            conn.commit()

    print(f"Done. checked={checked} refreshed={refreshed} failures={failures} dry_run={args.dry_run}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
