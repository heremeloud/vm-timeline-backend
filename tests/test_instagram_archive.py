import unittest
from unittest.mock import patch

from fastapi import HTTPException

from instagram_archive import (
    InstagramArchiveError,
    InstagramMediaSource,
    InstagramPostArchive,
    extract_instagram_post,
    instagram_shortcode,
)
from models import Author, Post
from routers.posts import PostArchiveRequest, archive_instagram_post


class InstagramArchiveTests(unittest.TestCase):
    def test_accepts_post_and_reel_urls(self):
        self.assertEqual(instagram_shortcode("https://www.instagram.com/p/ABC_123-/"), "ABC_123-")
        self.assertEqual(instagram_shortcode("https://instagram.com/reel/ReelCode/?utm_source=x"), "ReelCode")

    def test_rejects_non_instagram_and_profile_urls(self):
        with self.assertRaises(InstagramArchiveError):
            instagram_shortcode("https://example.com/p/ABC/")
        with self.assertRaises(InstagramArchiveError):
            instagram_shortcode("https://www.instagram.com/jaoyng/")

    def test_extracts_api_carousel_in_order(self):
        payload = {
            "items": [
                {
                    "code": "ABC123",
                    "caption": {"text": "Original caption"},
                    "carousel_media": [
                        {
                            "image_versions2": {
                                "candidates": [
                                    {"url": "https://small.cdninstagram.com/a.jpg", "width": 100, "height": 100},
                                    {"url": "https://large.cdninstagram.com/a.jpg", "width": 1000, "height": 1000},
                                ]
                            }
                        },
                        {
                            "video_versions": [
                                {"url": "https://video.cdninstagram.com/b.mp4", "width": 720, "height": 720}
                            ]
                        },
                    ],
                }
            ]
        }
        archive = extract_instagram_post(payload, "ABC123")
        self.assertIsNotNone(archive)
        self.assertEqual(archive.caption, "Original caption")
        self.assertEqual(
            [(item.kind, item.url) for item in archive.media],
            [
                ("image", "https://large.cdninstagram.com/a.jpg"),
                ("video", "https://video.cdninstagram.com/b.mp4"),
            ],
        )

    def test_extracts_graphql_sidecar_but_not_another_shortcode(self):
        payload = {
            "data": {
                "xdt_shortcode_media": {
                    "shortcode": "RIGHT",
                    "edge_media_to_caption": {"edges": [{"node": {"text": "Caption"}}]},
                    "edge_sidecar_to_children": {
                        "edges": [
                            {"node": {"display_url": "https://one.cdninstagram.com/1.jpg"}},
                            {"node": {"video_url": "https://two.cdninstagram.com/2.mp4"}},
                        ]
                    },
                },
                "suggested": {
                    "shortcode": "WRONG",
                    "display_url": "https://wrong.cdninstagram.com/x.jpg",
                },
            }
        }
        archive = extract_instagram_post(payload, "RIGHT")
        self.assertEqual(archive.caption, "Caption")
        self.assertEqual(len(archive.media), 2)
        self.assertIsNone(extract_instagram_post(payload, "MISSING"))


class FakeSession:
    def __init__(self, post, author):
        self.post = post
        self.author = author
        self.committed = False

    def get(self, model, object_id):
        if model is Post and object_id == self.post.id:
            return self.post
        if model is Author and object_id == self.author.id:
            return self.author
        return None

    def add(self, _value):
        return None

    def commit(self):
        self.committed = True

    def refresh(self, _value):
        return None


class FakeBrowser:
    archive = InstagramPostArchive(
        "ABC123",
        "Captured caption",
        (
            InstagramMediaSource("https://one.cdninstagram.com/1.jpg", "image"),
            InstagramMediaSource("https://two.cdninstagram.com/2.jpg", "image"),
        ),
    )

    def __init__(self, _cookie):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def capture(self, _url):
        return self.archive

    def download(self, _source, _max_bytes):
        return b"jpeg", "image/jpeg"


class InstagramArchiveEndpointTests(unittest.TestCase):
    def make_session(self):
        post = Post(
            id=42,
            platform="ig",
            content_type="post",
            external_url="https://www.instagram.com/p/ABC123/",
            external_id="ABC123",
            author_id=7,
            caption="Old caption",
            posted_at="2026-09-25",
        )
        return FakeSession(post, Author(id=7, name="Jaoying"))

    @patch("routers.posts.archive_cookie", return_value="sessionid=test")
    @patch("routers.posts.InstagramPostBrowser", FakeBrowser)
    @patch("routers.posts.media_router._resolve_public_object", side_effect=HTTPException(400))
    @patch("routers.posts.media_router.store_media_bytes")
    def test_archives_all_media_and_caption(self, store, _resolve, _cookie):
        store.side_effect = [
            {"url": "https://r2.example/1.jpg", "bucket": "bucket", "key": "1.jpg"},
            {"url": "https://r2.example/2.jpg", "bucket": "bucket", "key": "2.jpg"},
        ]
        session = self.make_session()

        result = archive_instagram_post(42, PostArchiveRequest(), session)

        self.assertTrue(session.committed)
        self.assertEqual(session.post.caption, "Captured caption")
        self.assertEqual(session.post.media_url, "https://r2.example/1.jpg")
        self.assertEqual(session.post.display_source, "r2")
        self.assertEqual(len(result["media_urls"]), 2)
        self.assertIn("https://r2.example/2.jpg", session.post.media_urls_json)

    @patch("routers.posts.archive_cookie", return_value="sessionid=test")
    @patch("routers.posts.InstagramPostBrowser", FakeBrowser)
    @patch("routers.posts.media_router._resolve_public_object", side_effect=HTTPException(400))
    @patch("routers.posts.media_router.delete_media_key")
    @patch("routers.posts.media_router.store_media_bytes")
    def test_rolls_back_partial_r2_upload(self, store, delete, _resolve, _cookie):
        store.side_effect = [
            {"url": "https://r2.example/1.jpg", "bucket": "bucket", "key": "1.jpg"},
            HTTPException(status_code=502, detail="R2 upload failed"),
        ]
        session = self.make_session()

        with self.assertRaises(HTTPException):
            archive_instagram_post(42, PostArchiveRequest(), session)

        self.assertFalse(session.committed)
        delete.assert_called_once_with("bucket", "1.jpg")


if __name__ == "__main__":
    unittest.main()
