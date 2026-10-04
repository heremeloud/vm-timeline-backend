import unittest

from sqlmodel import SQLModel, Session, create_engine

from models import Event, Post, Project, ProjectFilmingDay, ProjectEpisode
from routers.posts import get_event_post_candidates, get_project_post_candidates, get_project_related_post_counts


class EventRelatedPostTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_related_event_project_label_defaults_to_hidden(self):
        post = Post(
            platform="x",
            external_url="https://example.com/default-label",
            external_id="default-label",
            temp_author_name="Fan",
        )

        self.assertFalse(post.show_timeline_context)

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

    def test_project_filming_day_and_episode_hashtags_return_related_posts(self):
        project = Project(title="Girl Rules", category="series", is_visible=True)
        self.session.add(project)
        self.session.commit()
        self.session.refresh(project)
        self.session.add_all([
            ProjectFilmingDay(project_id=project.id, q_number=3, hashtag="GirlRulesQ3"),
            ProjectFilmingDay(project_id=project.id, q_number=4, hashtag="GirlRulesQ4"),
            ProjectEpisode(project_id=project.id, episode_number=2, hashtag="GirlRulesEP2"),
            Post(
                platform="x",
                external_url="https://example.com/q3",
                external_id="q3",
                temp_author_name="Fan",
                caption="Filming today #GirlRulesQ3",
            ),
            Post(
                platform="x",
                external_url="https://example.com/ep2",
                external_id="ep2",
                temp_author_name="Fan",
                caption="Episode time #GirlRulesEP2",
            ),
            Post(
                platform="x",
                external_url="https://example.com/not-q3",
                external_id="not-q3",
                temp_author_name="Fan",
                caption="A longer unrelated tag #GirlRulesQ30",
            ),
        ])
        self.session.commit()

        filming_posts = get_project_post_candidates(project.id, "#GirlRulesQ3", self.session)
        episode_posts = get_project_post_candidates(project.id, "GirlRulesEP2", self.session)
        counts = get_project_related_post_counts(project.id, self.session)

        self.assertEqual([post["external_id"] for post in filming_posts], ["q3"])
        self.assertEqual([post["external_id"] for post in episode_posts], ["ep2"])
        self.assertEqual(counts, {"GirlRulesQ3": 1, "GirlRulesQ4": 0, "GirlRulesEP2": 1})


if __name__ == "__main__":
    unittest.main()
