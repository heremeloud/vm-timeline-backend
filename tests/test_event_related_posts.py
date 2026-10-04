import json
import unittest

from sqlmodel import SQLModel, Session, create_engine

from fastapi import HTTPException

from models import Event, Post, Project, ProjectFilmingDay, ProjectEpisode, ProjectFittingWorkshop
from routers.posts import (
    _normalize_project_entry_links,
    create_post,
    get_event_post_candidates,
    get_project_post_candidates,
    get_project_related_post_counts,
    update_post,
)


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

    def test_returns_top_level_candidates_with_event_hashtag_even_if_hidden_from_the_timeline(self):
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

        # A post hidden from the public timeline still shows on related pages; only the
        # "show on related page" checkbox (and replies / unrelated posts) keep a post off them.
        self.assertEqual(sorted(post["external_id"] for post in results), ["hidden", "related"])

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
        self.assertEqual(counts, {
            "filming:3": 1, "GirlRulesQ3": 1,
            "filming:4": 0, "GirlRulesQ4": 0,
            "episodes:2": 1, "GirlRulesEP2": 1,
        })


    def test_fitting_and_workshop_hashtags_return_related_posts(self):
        project = Project(title="Girl Rules", slug="girl-rules", hashtag="GirlRules", category="series")
        self.session.add(project)
        self.session.commit()
        self.session.refresh(project)
        self.session.add_all([
            ProjectFittingWorkshop(project_id=project.id, kind="fitting", number=1, hashtag="GirlRulesF1"),
            ProjectFittingWorkshop(project_id=project.id, kind="workshop", number=2, hashtag="GirlRulesW2"),
        ])
        for external_id, caption in (("f1", "Fitting day #GirlRulesF1"), ("w2", "Workshop #GirlRulesW2")):
            self.session.add(Post(
                platform="x",
                external_url=f"https://example.com/{external_id}",
                external_id=external_id,
                temp_author_name="Fan",
                caption=caption,
                posted_at="2025-08-02",
            ))
        self.session.commit()

        fitting_posts = get_project_post_candidates(project.id, "GirlRulesF1", self.session)
        workshop_posts = get_project_post_candidates("girl-rules", "#GirlRulesW2", self.session)
        counts = get_project_related_post_counts(project.id, self.session)

        self.assertEqual([post["external_id"] for post in fitting_posts], ["f1"])
        self.assertEqual([post["external_id"] for post in workshop_posts], ["w2"])
        self.assertEqual(counts, {"fitting:1": 1, "GirlRulesF1": 1, "workshop:2": 1, "GirlRulesW2": 1})
        with self.assertRaises(HTTPException) as unknown:
            get_project_post_candidates(project.id, "GirlRulesF9", self.session)
        self.assertEqual(unknown.exception.status_code, 404)

    def test_posts_off_the_related_page_need_an_admin_token_to_be_listed(self):
        event = Event(name="Fan Sign", tags_json='["FanSign"]', start_date="2026-09-26")
        self.session.add(event)
        self.session.commit()
        self.session.refresh(event)
        self.session.add(Post(
            platform="x",
            external_url="https://example.com/excluded",
            external_id="excluded",
            temp_author_name="Fan",
            caption="#FanSign",
            posted_at="2026-09-26",
            show_on_related_page=False,
        ))
        self.session.commit()

        self.assertEqual(get_event_post_candidates(event.id, self.session), [])
        with self.assertRaises(HTTPException) as refused:
            get_event_post_candidates(event.id, self.session, include_hidden=True)
        self.assertEqual(refused.exception.status_code, 403)
        with self.assertRaises(HTTPException):
            get_event_post_candidates(event.id, self.session, include_hidden=True, authorization="Bearer not-a-token")


    def test_links_are_normalized_to_canonical_json(self):
        raw = [
            {"project_id": "6", "entry_type": "Workshop", "entry_number": 1},
            {"project_id": 6, "entry_type": "fitting", "entry_number": 1},
            {"project_id": 6, "entry_type": "fitting", "entry_number": 1},   # duplicate
            {"project_id": 6, "entry_type": "rehearsal", "entry_number": 1},  # unknown type
            {"project_id": 6, "entry_type": "fitting", "entry_number": -1},   # invalid number
            {"entry_type": "fitting", "entry_number": 2},                     # no project
            "nonsense",
        ]
        expected = '[{"entry_number":1,"entry_type":"fitting","project_id":6},{"entry_number":1,"entry_type":"workshop","project_id":6}]'
        self.assertEqual(_normalize_project_entry_links(raw), expected)
        self.assertEqual(_normalize_project_entry_links(expected), expected)
        self.assertEqual(_normalize_project_entry_links("not json"), "[]")
        self.assertEqual(_normalize_project_entry_links(None), "[]")

    def test_a_post_can_be_linked_to_rows_that_have_no_hashtag_or_keyword(self):
        project = Project(title="Girl Rules", slug="girl-rules", hashtag="GirlRules", category="series")
        self.session.add(project)
        self.session.commit()
        self.session.refresh(project)
        # a fitting and a workshop on the same day, neither with a hashtag
        self.session.add_all([
            ProjectFittingWorkshop(project_id=project.id, kind="fitting", number=1, date="2025-08-02"),
            ProjectFittingWorkshop(project_id=project.id, kind="workshop", number=1, date="2025-08-02"),
            ProjectFittingWorkshop(project_id=project.id, kind="workshop", number=2, date="2025-08-20"),
        ])
        self.session.commit()

        def link(*entries):
            return [{"project_id": project.id, "entry_type": kind, "entry_number": number} for kind, number in entries]

        both = create_post(Post(
            platform="x", external_url="https://example.com/both", external_id="both", temp_author_name="Fan",
            caption="Behind the scenes", posted_at="2025-08-02",
            project_entry_links_json=json.dumps(link(("fitting", 1), ("workshop", 1))),
        ), self.session)
        only_workshop = create_post(Post(
            platform="x", external_url="https://example.com/ws", external_id="ws", temp_author_name="Fan",
            caption="Workshop day", posted_at="2025-08-02",
            project_entry_links_json=json.dumps(link(("workshop", 1))),
        ), self.session)
        create_post(Post(
            platform="x", external_url="https://example.com/none", external_id="none", temp_author_name="Fan",
            caption="Unlinked", posted_at="2025-08-02",
        ), self.session)

        def related(entry_type, number):
            return sorted(p["external_id"] for p in get_project_post_candidates(
                project.id, "", self.session, entry_type=entry_type, entry_number=number,
            ))

        self.assertEqual(related("fitting", 1), ["both"])
        self.assertEqual(related("workshop", 1), ["both", "ws"])
        self.assertEqual(related("workshop", 2), [])
        with self.assertRaises(HTTPException) as missing:
            get_project_post_candidates(project.id, "", self.session, entry_type="fitting", entry_number=9)
        self.assertEqual(missing.exception.status_code, 404)
        # without a hashtag or entry there is nothing to list (and no error)
        self.assertEqual(get_project_post_candidates(project.id, "", self.session), [])

        counts = get_project_related_post_counts(project.id, self.session)
        self.assertEqual(counts, {"fitting:1": 1, "workshop:1": 2, "workshop:2": 0})

        # editing the links later replaces them
        update_post(both.id, {"project_entry_links_json": link(("workshop", 2))}, self.session)
        self.assertEqual(related("fitting", 1), [])
        self.assertEqual(related("workshop", 2), ["both"])
        self.assertEqual(only_workshop.project_entry_links_json, '[{"entry_number":1,"entry_type":"workshop","project_id":%d}]' % project.id)

    def test_a_row_is_counted_once_when_it_has_both_a_hashtag_and_an_explicit_link(self):
        project = Project(title="Girl Rules", slug="girl-rules", hashtag="GirlRules", category="series")
        self.session.add(project)
        self.session.commit()
        self.session.refresh(project)
        self.session.add(ProjectFittingWorkshop(project_id=project.id, kind="fitting", number=1, hashtag="GirlRulesF1"))
        self.session.commit()
        create_post(Post(
            platform="x", external_url="https://example.com/dup", external_id="dup", temp_author_name="Fan",
            caption="#GirlRulesF1", posted_at="2025-08-02",
            project_entry_links_json=json.dumps([{"project_id": project.id, "entry_type": "fitting", "entry_number": 1}]),
        ), self.session)

        self.assertEqual(get_project_related_post_counts(project.id, self.session), {"fitting:1": 1, "GirlRulesF1": 1})


if __name__ == "__main__":
    unittest.main()
