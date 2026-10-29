import unittest

from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine

from models import Post, Project, ProjectFilmingDay, ProjectFittingWorkshop
from sqlmodel import select
from routers.events import get_event_tag_index
from routers.posts import _links_from_json, _normalize_project_entry_links, invalidate_related_counts
from routers.projects import (
    ProjectFilmingDayInput,
    ProjectFittingWorkshopInput,
    _replace_series_metadata,
    _serialize_project,
)


class ProjectFittingWorkshopTests(unittest.TestCase):
    def setUp(self):
        invalidate_related_counts()
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.project = Project(title="Girl Rules", slug="girl-rules", hashtag="GirlRules", category="series", start_date="2026-03-09")
        self.session.add(self.project)
        self.session.commit()
        self.session.refresh(self.project)

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def save(self, rows, filming_days=None):
        _replace_series_metadata(self.session, self.project.id, filming_days, None, rows)
        self.session.commit()

    def test_rows_are_saved_cleaned_and_serialized_in_order(self):
        self.save([
            ProjectFittingWorkshopInput(kind="workshop", number=1, date=" 2025-08-20 ", hashtag="#Girl Rules W1", keyword=" GR WORKSHOP "),
            ProjectFittingWorkshopInput(kind="Fitting", number=2, hashtag="GirlRulesF2"),
            ProjectFittingWorkshopInput(kind="fitting", number=1, date="2025-08-02", hashtag="GirlRulesF1"),
        ])

        rows = _serialize_project(self.session, self.project)["fitting_workshops"]

        # old to new by date across types; the undated Fitting Day 2 goes last
        self.assertEqual([(r["kind"], r["number"]) for r in rows], [("fitting", 1), ("workshop", 1), ("fitting", 2)])
        workshop = rows[1]
        self.assertEqual((workshop["date"], workshop["hashtag"], workshop["keyword"]), ("2025-08-20", "GirlRulesW1", "GR WORKSHOP"))

    def test_days_are_listed_old_to_new_across_types(self):
        self.save([
            ProjectFittingWorkshopInput(kind="fitting", number=1, date="2025-08-02"),
            ProjectFittingWorkshopInput(kind="fitting", number=2, date="2025-08-25"),
            ProjectFittingWorkshopInput(kind="workshop", number=1, date="2025-08-10"),
            ProjectFittingWorkshopInput(kind="prep", number=1, date="2025-08-10"),
            ProjectFittingWorkshopInput(kind="workshop", number=2, date="2025-08-20"),
        ])

        rows = _serialize_project(self.session, self.project)["fitting_workshops"]
        self.assertEqual(
            [(r["kind"], r["number"]) for r in rows],
            [("fitting", 1), ("workshop", 1), ("prep", 1), ("workshop", 2), ("fitting", 2)],
        )

    def test_unticking_order_by_date_groups_days_by_type(self):
        self.project.fitting_workshops_by_date = False
        self.session.add(self.project)
        self.save([
            ProjectFittingWorkshopInput(kind="workshop", number=1, date="2025-08-01"),
            ProjectFittingWorkshopInput(kind="fitting", number=2, date="2025-08-05"),
            ProjectFittingWorkshopInput(kind="fitting", number=1, date="2025-08-20"),
        ])

        rows = _serialize_project(self.session, self.project)["fitting_workshops"]
        self.assertEqual([(r["kind"], r["number"]) for r in rows], [("fitting", 1), ("fitting", 2), ("workshop", 1)])

    def test_prep_days_are_a_third_type_listed_after_fittings_and_workshops(self):
        self.save([
            ProjectFittingWorkshopInput(kind="prep", number=1, hashtag="GirlRulesP1"),
            ProjectFittingWorkshopInput(kind="workshop", number=1),
            ProjectFittingWorkshopInput(kind="fitting", number=1),
            ProjectFittingWorkshopInput(kind="prep", number=2),
        ])

        rows = _serialize_project(self.session, self.project)["fitting_workshops"]
        self.assertEqual([(r["kind"], r["number"]) for r in rows], [("fitting", 1), ("workshop", 1), ("prep", 1), ("prep", 2)])

        prep = next(e for e in get_event_tag_index(self.session) if e.get("fitting_workshop_kind") == "prep" and e["tags"])
        self.assertEqual(prep["name"], "Girl Rules Prep Day 1")

    def test_same_number_is_allowed_across_kinds_but_not_within_one(self):
        self.save([
            ProjectFittingWorkshopInput(kind="fitting", number=1),
            ProjectFittingWorkshopInput(kind="workshop", number=1),
        ])
        with self.assertRaises(HTTPException) as duplicate:
            self.save([
                ProjectFittingWorkshopInput(kind="fitting", number=3),
                ProjectFittingWorkshopInput(kind="fitting", number=3),
            ])
        self.assertEqual(duplicate.exception.status_code, 400)
        with self.assertRaises(HTTPException):
            self.save([ProjectFittingWorkshopInput(kind="rehearsal", number=1)])
        with self.assertRaises(HTTPException):
            self.save([ProjectFittingWorkshopInput(kind="fitting", number=0)])

    def test_only_the_collections_that_are_sent_are_replaced(self):
        self.save([ProjectFittingWorkshopInput(kind="fitting", number=1)], filming_days=[ProjectFilmingDayInput(q_number=1, hashtag="GirlRulesQ1")])

        # Sending only filming days leaves fitting/workshop rows alone (and vice versa).
        _replace_series_metadata(self.session, self.project.id, [ProjectFilmingDayInput(q_number=2)], None, None)
        self.session.commit()
        data = _serialize_project(self.session, self.project)
        self.assertEqual([r["q_number"] for r in data["filming_days"]], [2])
        self.assertEqual([(r["kind"], r["number"]) for r in data["fitting_workshops"]], [("fitting", 1)])

        # An empty list clears just that collection.
        self.save([])
        data = _serialize_project(self.session, self.project)
        self.assertEqual(data["fitting_workshops"], [])
        self.assertEqual([r["q_number"] for r in data["filming_days"]], [2])

    def test_tag_index_lists_fitting_and_workshop_hashtags_as_project_entries(self):
        self.save([
            ProjectFittingWorkshopInput(kind="fitting", number=1, date="2025-08-02", hashtag="GirlRulesF1"),
            ProjectFittingWorkshopInput(kind="workshop", number=2, hashtag="GirlRulesW2"),
            ProjectFittingWorkshopInput(kind="workshop", number=3),  # no hashtag: listed, but with no tags
        ])

        index = get_event_tag_index(self.session)
        entries = {e["tags"][0]: e for e in index if e.get("is_fitting_workshop") and e["tags"]}
        untagged = [e for e in index if e.get("is_fitting_workshop") and not e["tags"]]

        self.assertEqual(set(entries), {"GirlRulesF1", "GirlRulesW2"})
        self.assertEqual([(e["fitting_workshop_kind"], e["fitting_workshop_number"]) for e in untagged], [("workshop", 3)])
        fitting = entries["GirlRulesF1"]
        self.assertTrue(fitting["is_project"])
        self.assertEqual((fitting["fitting_workshop_kind"], fitting["fitting_workshop_number"]), ("fitting", 1))
        self.assertEqual(fitting["name"], "Girl Rules Fitting Day 1")
        self.assertEqual(fitting["start_date"], "2025-08-02")
        self.assertEqual(entries["GirlRulesW2"]["name"], "Girl Rules Workshop Day 2")
        # a row without its own date falls back to the project's start date
        self.assertEqual(entries["GirlRulesW2"]["start_date"], "2026-03-09")


    def linked_post(self, *links):
        post = Post(
            platform="instagram", external_url=f"https://example.com/{len(links)}", external_id="x",
            project_entry_links_json=_normalize_project_entry_links(
                [{"project_id": project_id, "entry_type": entry_type, "entry_number": number} for project_id, entry_type, number in links]
            ),
        )
        self.session.add(post)
        self.session.commit()
        return post

    def links_of(self, post):
        self.session.refresh(post)
        return _links_from_json(post.project_entry_links_json)

    def row_ids(self):
        rows = self.session.exec(select(ProjectFittingWorkshop).where(ProjectFittingWorkshop.project_id == self.project.id)).all()
        return {(row.kind, row.number): row.id for row in rows}

    def test_renaming_a_day_relinks_its_posts(self):
        self.save([ProjectFittingWorkshopInput(kind="fitting", number=1), ProjectFittingWorkshopInput(kind="workshop", number=1)])
        pid = self.project.id
        post = self.linked_post((pid, "fitting", 1), (999, "fitting", 1))
        ids = self.row_ids()

        # Fitting Day 1 becomes Prep Day 2; Workshop Day 1 is untouched.
        self.save([
            ProjectFittingWorkshopInput(id=ids[("fitting", 1)], kind="prep", number=2),
            ProjectFittingWorkshopInput(id=ids[("workshop", 1)], kind="workshop", number=1),
        ])

        # Another project's link with the same type and number stays as it was.
        self.assertEqual(self.links_of(post), {(pid, "prep", 2), (999, "fitting", 1)})

    def test_swapping_two_days_swaps_their_posts(self):
        self.save([ProjectFittingWorkshopInput(kind="fitting", number=1), ProjectFittingWorkshopInput(kind="workshop", number=1)])
        pid = self.project.id
        fitting_post = self.linked_post((pid, "fitting", 1))
        workshop_post = self.linked_post((pid, "workshop", 1), (pid, "filming", 1))
        ids = self.row_ids()

        self.save([
            ProjectFittingWorkshopInput(id=ids[("fitting", 1)], kind="workshop", number=1),
            ProjectFittingWorkshopInput(id=ids[("workshop", 1)], kind="fitting", number=1),
        ])

        self.assertEqual(self.links_of(fitting_post), {(pid, "workshop", 1)})
        self.assertEqual(self.links_of(workshop_post), {(pid, "fitting", 1), (pid, "filming", 1)})

    def test_renumbering_a_q_day_relinks_its_posts(self):
        self.save([], filming_days=[ProjectFilmingDayInput(q_number=3)])
        pid = self.project.id
        post = self.linked_post((pid, "filming", 3))
        q3 = self.session.exec(select(ProjectFilmingDay).where(ProjectFilmingDay.project_id == pid)).one()

        self.save(None, filming_days=[ProjectFilmingDayInput(id=q3.id, q_number=4)])

        self.assertEqual(self.links_of(post), {(pid, "filming", 4)})

    def test_rows_sent_without_an_id_leave_links_alone(self):
        self.save([ProjectFittingWorkshopInput(kind="fitting", number=1)])
        pid = self.project.id
        post = self.linked_post((pid, "fitting", 1))

        self.save([ProjectFittingWorkshopInput(kind="workshop", number=1)])

        self.assertEqual(self.links_of(post), {(pid, "fitting", 1)})

if __name__ == "__main__":
    unittest.main()
