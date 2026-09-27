import unittest

from sqlmodel import SQLModel, Session, create_engine

from models import Event, Post
from routers.posts import get_event_post_candidates


class EventRelatedPostTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_returns_only_public_top_level_candidates_with_event_hashtag(self):
        event = Event(name="Fan Sign", tags_json='["FanSign"]', start_date="2026-09-26")
        self.session.add(event)
        self.session.commit()
        self.session.refresh(event)

        related = Post(
            platform="x",
            external_url="https://example.com/related",
            external_id="related",
            temp_author_name="Fan",
            caption="Today at #FanSign",
            posted_at="2026-09-26",
        )
        unrelated = Post(
            platform="x",
            external_url="https://example.com/unrelated",
            external_id="unrelated",
            temp_author_name="Fan",
            caption="A different post",
            posted_at="2026-09-26",
        )
        hidden = Post(
            platform="x",
            external_url="https://example.com/hidden",
            external_id="hidden",
            temp_author_name="Fan",
            caption="#FanSign",
            posted_at="2026-09-26",
            is_visible=False,
        )
        excluded_from_related_page = Post(
            platform="x",
            external_url="https://example.com/excluded",
            external_id="excluded",
            temp_author_name="Fan",
            caption="#FanSign",
            posted_at="2026-09-26",
            show_on_related_page=False,
        )
        self.session.add_all([related, unrelated, hidden, excluded_from_related_page])
        self.session.commit()

        results = get_event_post_candidates(event.id, self.session)

        self.assertEqual([post["external_id"] for post in results], ["related"])


if __name__ == "__main__":
    unittest.main()
