import unittest

from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine

from models import Project, ProjectFilmingDay
from routers.events import get_event_tag_index
from routers.posts import invalidate_related_counts
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

        self.assertEqual([(r["kind"], r["number"]) for r in rows], [("fitting", 1), ("fitting", 2), ("workshop", 1)])
        workshop = rows[2]
        self.assertEqual((workshop["date"], workshop["hashtag"], workshop["keyword"]), ("2025-08-20", "GirlRulesW1", "GR WORKSHOP"))

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


if __name__ == "__main__":
    unittest.main()
