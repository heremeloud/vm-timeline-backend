"""Authenticated Instagram post capture for durable R2 archives."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


MAX_ARCHIVE_ITEMS = 20


class InstagramArchiveError(RuntimeError):
    """An Instagram post could not be archived safely."""


class InstagramArchiveSessionError(InstagramArchiveError):
    """The configured Instagram session is missing, expired, or challenged."""


class InstagramArchiveRateLimitError(InstagramArchiveError):
    """Instagram asked the browser to stop making requests."""


@dataclass(frozen=True)
class InstagramMediaSource:
    url: str
    kind: str


@dataclass(frozen=True)
class InstagramPostArchive:
    shortcode: str
    caption: str | None
    media: tuple[InstagramMediaSource, ...]


def instagram_shortcode(post_url: str) -> str:
    parsed = urlparse((post_url or "").strip())
    if parsed.scheme != "https" or parsed.hostname not in {"instagram.com", "www.instagram.com"}:
        raise InstagramArchiveError("The source must be an https://www.instagram.com post URL.")
    match = re.fullmatch(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)/?", parsed.path)
    if not match:
        raise InstagramArchiveError("Only Instagram post, reel, and TV URLs can be archived.")
    return match.group(1)


def parse_cookie_header(cookie_header: str) -> list[dict[str, object]]:
    cookies: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw_part in cookie_header.split(";"):
        part = raw_part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if not name or name in seen or name.startswith("$"):
            continue
        seen.add(name)
        cookies.append(
            {
                "name": name,
                "value": value.strip(),
                "domain": ".instagram.com",
                "path": "/",
                "secure": True,
                "httpOnly": name == "sessionid",
                "sameSite": "Lax",
            }
        )
    return cookies


def _best_image_url(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    image_versions = value.get("image_versions2")
    candidates = image_versions.get("candidates") if isinstance(image_versions, dict) else None
    if isinstance(candidates, list):
        ranked = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            url = candidate.get("url")
            if isinstance(url, str) and url.startswith("https://"):
                ranked.append(
                    (
                        int(candidate.get("width") or 0) * int(candidate.get("height") or 0),
                        url,
                    )
                )
        if ranked:
            return max(ranked)[1]
    for key in ("display_url", "thumbnail_src"):
        url = value.get(key)
        if isinstance(url, str) and url.startswith("https://"):
            return url
    return None


def _best_video_url(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    versions = value.get("video_versions")
    if isinstance(versions, list):
        ranked = []
        for candidate in versions:
            if not isinstance(candidate, dict):
                continue
            url = candidate.get("url")
            if isinstance(url, str) and url.startswith("https://"):
                ranked.append(
                    (
                        int(candidate.get("width") or 0) * int(candidate.get("height") or 0),
                        url,
                    )
                )
        if ranked:
            return max(ranked)[1]
    url = value.get("video_url")
    return url if isinstance(url, str) and url.startswith("https://") else None


def _node_media(value: Any) -> InstagramMediaSource | None:
    video_url = _best_video_url(value)
    if video_url:
        return InstagramMediaSource(video_url, "video")
    image_url = _best_image_url(value)
    if image_url:
        return InstagramMediaSource(image_url, "image")
    return None


def _caption(value: dict[str, Any]) -> str | None:
    caption = value.get("caption")
    if isinstance(caption, dict):
        text = caption.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    elif isinstance(caption, str) and caption.strip():
        return caption.strip()

    edge = value.get("edge_media_to_caption")
    edges = edge.get("edges") if isinstance(edge, dict) else None
    if isinstance(edges, list):
        for item in edges:
            node = item.get("node") if isinstance(item, dict) else None
            text = node.get("text") if isinstance(node, dict) else None
            if isinstance(text, str) and text.strip():
                return text.strip()
    return None


def _media_items(value: dict[str, Any]) -> tuple[InstagramMediaSource, ...]:
    carousel = value.get("carousel_media")
    if isinstance(carousel, list):
        items = tuple(item for child in carousel if (item := _node_media(child)))
        if items:
            return items[:MAX_ARCHIVE_ITEMS]

    sidecar = value.get("edge_sidecar_to_children")
    edges = sidecar.get("edges") if isinstance(sidecar, dict) else None
    if isinstance(edges, list):
        items = []
        for edge in edges:
            node = edge.get("node") if isinstance(edge, dict) else None
            item = _node_media(node)
            if item:
                items.append(item)
        if items:
            return tuple(items[:MAX_ARCHIVE_ITEMS])

    single = _node_media(value)
    return (single,) if single else ()


def extract_instagram_post(payload: Any, shortcode: str) -> InstagramPostArchive | None:
    """Find the exact shortcode in captured JSON and extract its ordered media."""

    def visit(value: Any) -> InstagramPostArchive | None:
        if isinstance(value, dict):
            identifier = value.get("code") or value.get("shortcode")
            if identifier == shortcode:
                media = _media_items(value)
                if media:
                    return InstagramPostArchive(shortcode, _caption(value), media)
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


def _trusted_instagram_media_url(url: str) -> bool:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(
        hostname == suffix or hostname.endswith(f".{suffix}")
        for suffix in ("cdninstagram.com", "fbcdn.net")
    )


class InstagramPostBrowser:
    def __init__(self, cookie_header: str, timeout_seconds: int = 45) -> None:
        cookies = parse_cookie_header(cookie_header)
        if "sessionid" not in {str(cookie["name"]) for cookie in cookies}:
            raise InstagramArchiveSessionError("IG_COOKIE must contain sessionid.")
        self.cookies = cookies
        self.timeout_ms = timeout_seconds * 1000
        self._playwright = None
        self._browser = None
        self.context = None

    def __enter__(self) -> "InstagramPostBrowser":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise InstagramArchiveError("Playwright is not installed on the backend.") from exc

        self._playwright = sync_playwright().start()
        channel = (os.getenv("INSTAGRAM_BROWSER_CHANNEL") or "").strip()
        options: dict[str, object] = {"headless": True}
        if channel:
            options["channel"] = channel
        try:
            self._browser = self._playwright.chromium.launch(**options)
            self.context = self._browser.new_context(locale="en-US")
            self.context.set_default_timeout(self.timeout_ms)
            self.context.set_default_navigation_timeout(self.timeout_ms)
            self.context.add_cookies(self.cookies)
        except Exception as exc:
            self._playwright.stop()
            raise InstagramArchiveError(f"Could not start the archive browser: {exc}") from exc
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.context is not None:
            self.context.close()
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()

    def capture(self, post_url: str) -> InstagramPostArchive:
        assert self.context is not None
        shortcode = instagram_shortcode(post_url)
        canonical_url = f"https://www.instagram.com/p/{shortcode}/"
        if "/reel/" in post_url:
            canonical_url = f"https://www.instagram.com/reel/{shortcode}/"
        elif "/tv/" in post_url:
            canonical_url = f"https://www.instagram.com/tv/{shortcode}/"

        page = self.context.new_page()
        matches: list[InstagramPostArchive] = []
        saw_rate_limit = False

        def inspect_response(response: Any) -> None:
            nonlocal saw_rate_limit
            try:
                if response.status == 429:
                    saw_rate_limit = True
                    return
                content_type = (response.headers.get("content-type") or "").lower()
                if "json" not in content_type:
                    return
                found = extract_instagram_post(response.json(), shortcode)
                if found:
                    matches.append(found)
            except Exception:
                return

        page.on("response", inspect_response)
        try:
            response = page.goto(canonical_url, wait_until="domcontentloaded")
            page.wait_for_timeout(3500)
            if saw_rate_limit or (response is not None and response.status == 429):
                raise InstagramArchiveRateLimitError("Instagram rate-limited the archive request.")
            current_url = page.url.lower()
            if "/accounts/login" in current_url:
                raise InstagramArchiveSessionError("Instagram redirected to login; refresh IG_COOKIE.")
            if "/challenge" in current_url or "/checkpoint" in current_url:
                raise InstagramArchiveSessionError("Instagram requires a security challenge.")
            body_text = page.locator("body").inner_text().casefold()
            if "please wait a few minutes" in body_text or "try again later" in body_text:
                raise InstagramArchiveRateLimitError("Instagram displayed a temporary rate limit.")
            if not matches:
                for script_text in page.locator('script[type="application/json"]').all_text_contents():
                    if shortcode not in script_text:
                        continue
                    try:
                        found = extract_instagram_post(json.loads(script_text), shortcode)
                    except (TypeError, ValueError):
                        continue
                    if found:
                        matches.append(found)
                        break
            if not matches:
                raise InstagramArchiveError(
                    "Instagram loaded the page but did not expose verified post media."
                )
            return matches[0]
        except (InstagramArchiveError, InstagramArchiveSessionError, InstagramArchiveRateLimitError):
            raise
        except Exception as exc:
            raise InstagramArchiveError(
                f"Instagram browser capture failed: {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            try:
                page.close()
            except Exception:
                pass

    def download(self, source: InstagramMediaSource, max_bytes: int) -> tuple[bytes, str]:
        assert self.context is not None
        if not _trusted_instagram_media_url(source.url):
            raise InstagramArchiveError("Instagram returned an untrusted media hostname.")
        try:
            response = self.context.request.get(
                source.url,
                headers={"Referer": "https://www.instagram.com/"},
                timeout=self.timeout_ms,
            )
        except Exception as exc:
            raise InstagramArchiveError(f"Media download failed: {exc}") from exc
        if response.status == 429:
            raise InstagramArchiveRateLimitError("Instagram rate-limited a media download.")
        if not response.ok:
            raise InstagramArchiveError(f"Media download returned HTTP {response.status}.")
        content_length = int(response.headers.get("content-length") or 0)
        if content_length > max_bytes:
            raise InstagramArchiveError("An Instagram media item exceeds the upload size limit.")
        body = response.body()
        if not body:
            raise InstagramArchiveError("Instagram returned an empty media item.")
        if len(body) > max_bytes:
            raise InstagramArchiveError("An Instagram media item exceeds the upload size limit.")
        content_type = (response.headers.get("content-type") or "").partition(";")[0].lower()
        expected_prefix = "video/" if source.kind == "video" else "image/"
        if not content_type.startswith(expected_prefix):
            raise InstagramArchiveError(
                f"Instagram returned {content_type or 'unknown data'} for a {source.kind}."
            )
        return body, content_type


def extension_for_media(content_type: str) -> str:
    extensions = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/webm": ".webm",
    }
    extension = extensions.get(content_type.lower())
    if not extension:
        raise InstagramArchiveError(f"Unsupported Instagram media type: {content_type}")
    return extension


def archive_cookie() -> str:
    cookie = (os.getenv("IG_COOKIE") or "").strip()
    if not cookie:
        raise InstagramArchiveSessionError("IG_COOKIE is not configured on the backend.")
    return cookie
