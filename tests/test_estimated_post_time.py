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

    def ordered_ids(self):
        query = select(Post).where(Post.parent_id == None)
        return [p.id for p in self.session.exec(_order_posts(query, "newest")).all()]

    def test_stories_with_estimated_times_sort_by_time(self):
        # The older story gets the LATER estimate; a new post lands at the top of a manual day, so if the
        # estimates were ignored the newer-created story would come first.
        older = self.story(estimated_posted_at_utc="2026-10-12T09:00:00Z")
        newer = self.story(estimated_posted_at_utc="2026-10-12T01:00:00Z")
        self.assertEqual(self.ordered_ids(), [self.id_of(older), self.id_of(newer)])

    def test_a_tweet_is_ordered_by_time_among_estimated_stories(self):
        story_a = self.story(estimated_posted_at_utc="2026-10-12T03:31:00Z")
        story_b = self.story(estimated_posted_at_utc="2026-10-12T03:33:00Z")
        tweet = create_post(Post(
            author_id=self.author.id, platform="x", content_type="post", external_url="https://x.com/a/status/1",
            external_id="tweet-1", posted_at="2026-10-12", posted_at_utc="2026-10-12T03:24:41Z",
        ), self.session)
        # newest first: 03:33 story, 03:31 story, then the older 03:24 tweet (created last, but not on top)
        self.assertEqual(self.ordered_ids(), [self.id_of(story_b), self.id_of(story_a), self.id_of(tweet)])

    def test_a_story_without_any_time_keeps_the_day_in_manual_order(self):
        timed = self.story(estimated_posted_at_utc="2026-10-12T09:00:00Z")
        untimed = self.story()  # newest in manual order goes to the top of the day
        self.assertEqual(self.ordered_ids(), [self.id_of(untimed), self.id_of(timed)])

    @staticmethod
    def id_of(post):
        return post["id"] if isinstance(post, dict) else post.id


if __name__ == "__main__":
    unittest.main()
