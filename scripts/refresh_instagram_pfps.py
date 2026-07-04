#!/usr/bin/env python3
"""Refresh Instagram profile photos into local author uploads.

Run from vm-timeline-backend:
    python3 scripts/refresh_instagram_pfps.py --dry-run
    python3 scripts/refresh_instagram_pfps.py
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
os.chdir(BACKEND_DIR)


DB_PATH = BACKEND_DIR / "vm-social.db"
UPLOAD_DIR = BACKEND_DIR / "uploads" / "authors"
PUBLIC_PREFIX = "/static/authors"
DEFAULT_TIMEOUT = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "application/json",
    "X-IG-App-ID": "936619743392459",
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


def request_bytes(url: str, headers: dict[str, str], timeout: int) -> tuple[bytes, str]:
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as response:
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


def extract_profile_photo_url(page_html: str) -> str | None:
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


def fetch_profile_photo_url(username: str, timeout: int) -> str | None:
    api_url = f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}"
    try:
        body, _ = request_bytes(api_url, API_HEADERS, timeout)
        data = json.loads(body.decode("utf-8", errors="replace"))
        user = data.get("data", {}).get("user", {})
        return user.get("profile_pic_url_hd") or user.get("profile_pic_url")
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
        pass

    profile_url = f"https://www.instagram.com/{username}/"
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
) -> tuple[bool, str]:
    username = normalize_instagram_username(author.instagram_url or "")
    if not username:
        return False, "skip: invalid instagram_url"

    if not should_refresh(author, force):
        return False, f"skip: already local ({author.ig_pfp_url})"

    profile_photo_url = fetch_profile_photo_url(username, timeout)
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
    parser.add_argument("--force", action="store_true", help="Refresh even when ig_pfp_url already points to a local /static/authors file.")
    parser.add_argument("--author-id", type=int, help="Refresh one author by ID.")
    parser.add_argument("--name", help="Refresh authors whose name contains this text.")
    parser.add_argument("--limit", type=positive_int, help="Stop after checking this many matching authors.")
    parser.add_argument("--timeout", type=positive_int, default=DEFAULT_TIMEOUT, help=f"HTTP timeout in seconds. Default: {DEFAULT_TIMEOUT}.")
    return parser


def limited(rows: Iterable[Author], limit: int | None) -> Iterable[Author]:
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

    checked = 0
    changed = 0
    failures = 0

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        authors = select_authors(conn, args.author_id, args.name)
        for author in limited(authors, args.limit):
            checked += 1
            label = f"#{author.id} {author.name}"
            try:
                ok, message = refresh_author(conn, author, args.dry_run, args.force, args.timeout)
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                ok = False
                message = f"failed: {exc}"

            if ok:
                changed += 1
            elif message.startswith("failed:"):
                failures += 1

            print(f"{label}: {message}")

        if changed and not args.dry_run:
            conn.commit()

    print(f"Done. checked={checked} refreshed={changed} failures={failures} dry_run={args.dry_run}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
