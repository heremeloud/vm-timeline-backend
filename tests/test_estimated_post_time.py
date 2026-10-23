import unittest

from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine, select

from models import Author, Post
from routers.posts import _order_posts, create_post, update_post


class EstimatedPostTimeTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.author = Author(name="View")
        self.session.add(self.author)
        self.session.commit()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def story(self, **fields):
        self.count = getattr(self, "count", 0) + 1
        post = Post(
            author_id=self.author.id, platform="ig", content_type="story", external_url="",
            external_id=f"story-{self.count}", posted_at="2026-10-12", **fields,
        )
        return create_post(post, self.session)

    def test_estimate_is_saved_normalised_to_utc(self):
        saved = self.story(estimated_posted_at_utc="2026-10-12T17:30:00+07:00")
        post = self.session.get(Post, saved["id"] if isinstance(saved, dict) else saved.id)
        self.assertEqual(post.estimated_posted_at_utc, "2026-10-12T10:30:00.000Z")
        self.assertIsNone(post.posted_at_utc)

    def test_estimate_can_be_updated_and_cleared(self):
        saved = self.story()
        post_id = saved["id"] if isinstance(saved, dict) else saved.id
        update_post(post_id, {"estimated_posted_at_utc": "2026-10-12T03:00:00Z"}, self.session)
        self.assertEqual(self.session.get(Post, post_id).estimated_posted_at_utc, "2026-10-12T03:00:00.000Z")
        update_post(post_id, {"estimated_posted_at_utc": None}, self.session)
        self.assertIsNone(self.session.get(Post, post_id).estimated_posted_at_utc)

    def test_invalid_estimate_is_rejected(self):
        with self.assertRaises(HTTPException):
            self.story(estimated_posted_at_utc="yesterday")
        with self.assertRaises(HTTPException):
            self.story(estimated_posted_at_utc="2026-10-12T10:00:00")

    def test_estimate_never_changes_the_manual_story_order(self):
        # A new story lands at the top of its day (manual order). Give the older story the LATER estimate:
        # if estimates affected ordering, the older story would come first.
        older = self.story(estimated_posted_at_utc="2026-10-12T09:00:00Z")
        newer = self.story(estimated_posted_at_utc="2026-10-12T01:00:00Z")
        older_id = older["id"] if isinstance(older, dict) else older.id
        newer_id = newer["id"] if isinstance(newer, dict) else newer.id
        ordered = [p.id for p in self.session.exec(_order_posts(select(Post).where(Post.parent_id == None), "newest")).all()]
        self.assertEqual(ordered, [newer_id, older_id])


if __name__ == "__main__":
    unittest.main()
