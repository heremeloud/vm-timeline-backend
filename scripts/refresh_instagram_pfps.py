#!/usr/bin/env python3
"""Refresh Instagram author avatars through an authenticated browser.

Run from ``vm-timeline-backend``::

    python3 scripts/refresh_instagram_pfps.py --author-id 3 --force --dry-run
    python3 scripts/refresh_instagram_pfps.py --author-id 3 --force

Set ``IG_COOKIE`` in ``.env`` to the complete Cookie request-header value copied
from a logged-in Instagram browser request. The cookie is installed into a real
browser context; it is never printed or written to another file.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from dotenv import load_dotenv


SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
DB_PATH = BACKEND_DIR / "vm-social.db"
UPLOAD_DIR = BACKEND_DIR / "uploads" / "authors"
PUBLIC_PREFIX = "/static/authors"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_DELAY_SECONDS = 8.0
MAX_IMAGE_BYTES = 10 * 1024 * 1024

os.chdir(BACKEND_DIR)
load_dotenv(BACKEND_DIR / ".env")


@dataclass
class AuthorRow:
    id: int
    name: str
    instagram_url: str | None
    ig_pfp_url: str | None


@dataclass(frozen=True)
class AvatarCandidate:
    url: str
    alt: str
    width: int = 0
    height: int = 0


class InstagramLookupError(RuntimeError):
    """A profile could not be read safely."""


class InstagramSessionError(InstagramLookupError):
    """The supplied browser session is logged out or challenged."""


class InstagramRateLimitError(InstagramLookupError):
    """Instagram asked the browser to slow down."""


def normalize_instagram_username(instagram_url: str) -> str | None:
    text = (instagram_url or "").strip()
    if not text:
        return None

    if text.startswith("@"):
        username = text[1:].split("/", 1)[0]
    else:
        if not re.match(r"^https?://", text, re.IGNORECASE):
            text = f"https://instagram.com/{text.lstrip('/')}"
        parsed = urlparse(text)
        if parsed.hostname not in {"instagram.com", "www.instagram.com"}:
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            return None
        if parts[0].lower() in {"accounts", "explore", "p", "reel", "reels", "stories"}:
            return None
        username = parts[0]

    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", username):
        return None
    if username.startswith(".") or username.endswith(".") or ".." in username:
        return None
    return username


def parse_cookie_header(cookie_header: str) -> list[dict[str, object]]:
    """Convert an HTTP Cookie header into Playwright cookie dictionaries."""
    cookies: list[dict[str, object]] = []
    seen: set[str] = set()
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or name.startswith("$") or name in seen:
            continue
        seen.add(name)
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": ".instagram.com",
                "path": "/",
                "secure": True,
                "httpOnly": name == "sessionid",
                "sameSite": "Lax",
            }
        )
    return cookies


def load_cookie_header(cookie_file: str | None) -> str:
    if cookie_file:
        value = Path(cookie_file).expanduser().read_text(encoding="utf-8").strip()
    else:
        value = os.environ.get("IG_COOKIE", "").strip()
    if not value:
        raise InstagramSessionError(
            "No Instagram cookie found. Set IG_COOKIE in .env or use --cookie-file."
        )
    names = {cookie["name"] for cookie in parse_cookie_header(value)}
    if "sessionid" not in names:
        raise InstagramSessionError("The Instagram cookie does not contain sessionid.")
    return value


def find_matching_profile_photo(payload: Any, username: str) -> str | None:
    """Find an avatar only inside a JSON user object naming the expected user."""
    expected = username.casefold()

    def visit(value: Any) -> str | None:
        if isinstance(value, dict):
            actual = value.get("username")
            if isinstance(actual, str) and actual.casefold() == expected:
                for key in ("profile_pic_url_hd", "profile_pic_url", "profile_picture_url"):
                    candidate = value.get(key)
                    if isinstance(candidate, str) and candidate.startswith("https://"):
                        return candidate
            for child in value.values():
                found = visit(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = visit(child)
                if found:
                    return found
        return None

    return visit(payload)


def choose_verified_avatar(candidates: Iterable[AvatarCandidate], username: str) -> str | None:
    """Accept only an image whose accessible label identifies this username."""
    expected = username.casefold()
    ranked: list[tuple[int, int, str]] = []
    for candidate in candidates:
        alt = " ".join(candidate.alt.casefold().split())
        if not re.search(
            rf"(?<![a-z0-9._]){re.escape(expected)}(?![a-z0-9._])",
            alt,
        ):
            continue
        if "profile picture" not in alt and "profile photo" not in alt:
            continue
        if not candidate.url.startswith("https://"):
            continue
        area = max(candidate.width, 0) * max(candidate.height, 0)
        exact_prefix = int(alt.startswith(expected) or alt.startswith(f"{expected}'s"))
        ranked.append((exact_prefix, area, candidate.url))
    return max(ranked, default=None)[2] if ranked else None


def image_extension(content_type: str, body: bytes) -> str:
    media_type = content_type.partition(";")[0].strip().lower()
    extensions = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    if media_type not in extensions:
        raise InstagramLookupError(
            f"Avatar download returned {media_type or 'an unknown content type'}, not an image."
        )
    signatures = {
        ".jpg": (b"\xff\xd8\xff",),
        ".png": (b"\x89PNG\r\n\x1a\n",),
        ".webp": (b"RIFF",),
        ".gif": (b"GIF87a", b"GIF89a"),
    }
    extension = extensions[media_type]
    if not any(body.startswith(signature) for signature in signatures[extension]):
        raise InstagramLookupError("Avatar response claimed to be an image but had invalid bytes.")
    if extension == ".webp" and body[8:12] != b"WEBP":
        raise InstagramLookupError("Avatar response had an invalid WebP signature.")
    return extension


class InstagramBrowser:
    """Small Playwright adapter kept behind a context manager for clean shutdown."""

    def __init__(
        self,
        cookie_header: str,
        timeout_seconds: int,
        browser_channel: str | None,
        headful: bool,
    ) -> None:
        self.cookie_header = cookie_header
        self.timeout_ms = timeout_seconds * 1000
        self.browser_channel = browser_channel
        self.headful = headful
        self._playwright = None
        self._browser = None
        self.context = None

    def __enter__(self) -> "InstagramBrowser":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Run: pip install -r requirements.txt"
            ) from exc

        self._playwright = sync_playwright().start()
        launch_options: dict[str, object] = {"headless": not self.headful}
        if self.browser_channel:
            launch_options["channel"] = self.browser_channel
        try:
            self._browser = self._playwright.chromium.launch(**launch_options)
        except Exception as exc:
            self._playwright.stop()
            hint = (
                f"Could not launch browser channel {self.browser_channel!r}."
                if self.browser_channel
                else "Could not launch Playwright Chromium. Run: playwright install chromium"
            )
            raise RuntimeError(f"{hint} {exc}") from exc

        self.context = self._browser.new_context(locale="en-US")
        self.context.set_default_timeout(self.timeout_ms)
        self.context.set_default_navigation_timeout(self.timeout_ms)
        self.context.add_cookies(parse_cookie_header(self.cookie_header))
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.context is not None:
            self.context.close()
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()

    def find_avatar(self, username: str) -> str:
        assert self.context is not None
        page = self.context.new_page()
        captured: list[str] = []

        def inspect_response(response: Any) -> None:
            try:
                if response.status == 429:
                    return
                content_type = (response.headers.get("content-type") or "").lower()
                if "json" not in content_type:
                    return
                found = find_matching_profile_photo(response.json(), username)
                if found:
                    captured.append(found)
            except Exception:
                return

        page.on("response", inspect_response)
        try:
            response = page.goto(
                f"https://www.instagram.com/{username}/",
                wait_until="domcontentloaded",
            )
            if response is not None and response.status == 429:
                raise InstagramRateLimitError("Instagram returned HTTP 429; stop and retry later.")

            page.wait_for_timeout(2500)
            current_url = page.url.lower()
            if "/accounts/login" in current_url:
                raise InstagramSessionError("Instagram redirected to login; refresh IG_COOKIE.")
            if "/challenge" in current_url or "/checkpoint" in current_url:
                raise InstagramSessionError("Instagram requires a browser security challenge.")

            page_text = page.locator("body").inner_text(timeout=self.timeout_ms).casefold()
            if "please wait a few minutes" in page_text or "try again later" in page_text:
                raise InstagramRateLimitError("Instagram displayed a temporary rate-limit page.")

            if captured:
                return captured[0]

            raw_candidates = page.locator("img[alt]").evaluate_all(
                """images => images.map(image => ({
                    url: image.currentSrc || image.src || '',
                    alt: image.alt || '',
                    width: image.naturalWidth || image.width || 0,
                    height: image.naturalHeight || image.height || 0
                }))"""
            )
            candidates = [
                AvatarCandidate(
                    url=str(item.get("url", "")),
                    alt=str(item.get("alt", "")),
                    width=int(item.get("width", 0) or 0),
                    height=int(item.get("height", 0) or 0),
                )
                for item in raw_candidates
                if isinstance(item, dict)
            ]
            found = choose_verified_avatar(candidates, username)
            if found:
                return found
            raise InstagramLookupError(
                "The authenticated page loaded, but no avatar explicitly identified this username."
            )
        except (InstagramLookupError, InstagramSessionError, InstagramRateLimitError):
            raise
        except Exception as exc:
            raise InstagramLookupError(
                f"Browser lookup failed: {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            try:
                page.close()
            except Exception:
                pass

    def download_avatar(self, url: str) -> tuple[bytes, str]:
        assert self.context is not None
        try:
            response = self.context.request.get(
                url,
                headers={"Referer": "https://www.instagram.com/"},
                timeout=self.timeout_ms,
            )
        except Exception as exc:
            raise InstagramLookupError(
                f"Avatar download failed: {type(exc).__name__}: {exc}"
            ) from exc
        if response.status == 429:
            raise InstagramRateLimitError("Instagram's image host returned HTTP 429.")
        if not response.ok:
            raise InstagramLookupError(f"Avatar download returned HTTP {response.status}.")
        body = response.body()
        if not body:
            raise InstagramLookupError("Avatar download returned an empty response.")
        if len(body) > MAX_IMAGE_BYTES:
            raise InstagramLookupError("Avatar download exceeded the 10 MB safety limit.")
        extension = image_extension(response.headers.get("content-type", ""), body)
        return body, extension


def should_refresh(author: AuthorRow, force: bool) -> bool:
    current = (author.ig_pfp_url or "").strip()
    return force or not current or current.startswith(("http://", "https://"))


def select_authors(
    conn: sqlite3.Connection, author_id: int | None, name: str | None
) -> list[AuthorRow]:
    query = """
        SELECT id, name, instagram_url, ig_pfp_url
        FROM author
        WHERE instagram_url IS NOT NULL AND trim(instagram_url) != ''
    """
    params: list[object] = []
    if author_id is not None:
        query += " AND id = ?"
        params.append(author_id)
    query += " ORDER BY id"
    rows = [AuthorRow(*row) for row in conn.execute(query, params).fetchall()]
    if name:
        needle = name.casefold()
        rows = [row for row in rows if needle in row.name.casefold()]
    return rows


def limited(rows: Iterable[AuthorRow], limit: int | None) -> Iterable[AuthorRow]:
    for index, row in enumerate(rows):
        if limit is not None and index >= limit:
            return
        yield row


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download verified Instagram profile photos through a logged-in browser."
    )
    parser.add_argument("--author-id", type=int, help="Refresh one author by database ID.")
    parser.add_argument("--name", help="Refresh authors whose name contains this text.")
    parser.add_argument("--limit", type=positive_int, help="Check at most this many authors.")
    parser.add_argument(
        "--force",
        "--redownload-all",
        dest="force",
        action="store_true",
        help="Replace an existing local avatar.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Find the avatar without downloading or updating the database.")
    parser.add_argument("--cookie-file", help="File containing a complete Instagram Cookie header; defaults to IG_COOKIE.")
    parser.add_argument("--timeout", type=positive_int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--delay", type=nonnegative_float, default=DEFAULT_DELAY_SECONDS)
    parser.add_argument(
        "--browser-channel",
        default="",
        help="Optional Playwright browser channel; bundled Chromium is used by default.",
    )
    parser.add_argument("--headful", action="store_true", help="Show the automated browser for troubleshooting.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        cookie_header = load_cookie_header(args.cookie_file)
    except (OSError, InstagramSessionError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    with sqlite3.connect(DB_PATH) as conn:
        authors = list(limited(select_authors(conn, args.author_id, args.name), args.limit))
        if not authors:
            print("No matching authors with Instagram URLs.")
            return 0

        checked = refreshed = failures = 0
        try:
            with InstagramBrowser(
                cookie_header=cookie_header,
                timeout_seconds=args.timeout,
                browser_channel=args.browser_channel or None,
                headful=args.headful,
            ) as instagram:
                for index, author in enumerate(authors):
                    label = f"#{author.id} {author.name}"
                    if not should_refresh(author, args.force):
                        print(f"{label}: skip: already local ({author.ig_pfp_url})")
                        continue

                    checked += 1
                    username = normalize_instagram_username(author.instagram_url or "")
                    if not username:
                        failures += 1
                        print(f"{label}: failed: invalid Instagram URL")
                        continue

                    try:
                        avatar_url = instagram.find_avatar(username)
                        if args.dry_run:
                            refreshed += 1
                            print(f"{label}: dry-run: verified @{username} avatar")
                        else:
                            image, extension = instagram.download_avatar(avatar_url)
                            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                            filename = f"ig-{author.id}{extension}"
                            destination = UPLOAD_DIR / filename
                            temporary = destination.with_suffix(destination.suffix + ".tmp")
                            temporary.write_bytes(image)
                            temporary.replace(destination)
                            local_url = f"{PUBLIC_PREFIX}/{filename}"
                            conn.execute(
                                "UPDATE author SET ig_pfp_url = ? WHERE id = ?",
                                (local_url, author.id),
                            )
                            conn.commit()
                            refreshed += 1
                            print(f"{label}: updated: {local_url}")
                    except (InstagramRateLimitError, InstagramSessionError) as exc:
                        failures += 1
                        print(f"{label}: failed: {exc}")
                        print("Stopped to protect the Instagram session; no further profiles were requested.")
                        break
                    except InstagramLookupError as exc:
                        failures += 1
                        print(f"{label}: failed: {exc}")

                    if args.delay and index < len(authors) - 1:
                        time.sleep(args.delay)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2

    print(
        f"Done. checked={checked} refreshed={refreshed} "
        f"failures={failures} dry_run={args.dry_run}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
