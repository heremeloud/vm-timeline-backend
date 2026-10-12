import unittest

from sqlmodel import SQLModel, Session, create_engine, select

from fastapi import HTTPException

from models import Author, Event, EventAuthorLink, Project, SearchSettings
from routers.events import count_admin_events
from routers.search_settings import SearchSettingsPayload, get_search_settings, update_search_settings
from search_rules import MAX_SEARCH_LENGTH, event_search_filter, project_search_filter, search_words


class EventSearchTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        for name, english, keyword, tags in [
            ("ViewMim Fan Meet Bangkok", None, "fanmeet", '["ViewMim", "วิวมิ้ม", "viewmimfanmeet"]'),
            ("Press Tour Japan", "Japan Press", "press", '["ViewMim", "วิวมิ้ม"]'),
            ("100% Live Show", None, None, '["ViewMim", "วิวมิ้ม"]'),
            ("Another_Event", None, None, '["ViewMim", "วิวมิ้ม"]'),
        ]:
            self.session.add(Event(name=name, english_name=english, keyword=keyword, tags_json=tags))
        self.session.commit()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def search(self, term):
        clause = event_search_filter(self.session, term)
        query = select(Event.name) if clause is None else select(Event.name).where(clause)
        return sorted(self.session.exec(query).all())

    def test_words_match_in_any_order(self):
        self.assertEqual(self.search("bangkok fan"), ["ViewMim Fan Meet Bangkok"])

    def test_hashtag_prefix_is_ignored_for_tags(self):
        self.assertEqual(self.search("#viewmimfanmeet"), ["ViewMim Fan Meet Bangkok"])

    def test_typos_still_match(self):
        self.assertEqual(self.search("bangkoc"), ["ViewMim Fan Meet Bangkok"])
        self.assertEqual(self.search("japann pres"), ["Press Tour Japan"])

    def test_short_words_are_not_fuzzy(self):
        self.assertEqual(self.search("fen"), [])

    def test_identity_hashtags_do_not_match_every_event(self):
        for term in ("mim", "ViewMim", "#วิวมิ้ม", "วิวมิ้ม"):
            self.assertEqual(self.search(term), ["ViewMim Fan Meet Bangkok"] if term in ("mim", "ViewMim") else [], term)

    def test_identity_words_still_match_names_and_keywords(self):
        self.session.add(Event(name="วิวมิ้ม Birthday", keyword="วิวมิ้ม", tags_json='["ViewMim"]'))
        self.session.commit()
        self.assertEqual(self.search("วิวมิ้ม"), ["วิวมิ้ม Birthday"])

    def test_non_identity_tags_still_match(self):
        self.assertEqual(self.search("viewmimfanmeet"), ["ViewMim Fan Meet Bangkok"])

    def test_like_wildcards_match_literally(self):
        self.assertEqual(self.search("%"), ["100% Live Show"])
        self.assertEqual(self.search("_"), ["Another_Event"])

    def test_sql_injection_text_is_just_text(self):
        self.assertEqual(self.search("'; DROP TABLE event; --"), [])
        self.assertEqual(len(self.session.exec(select(Event)).all()), 4)

    def test_blank_search_adds_no_filter(self):
        self.assertIsNone(event_search_filter(self.session, "   "))

    def test_input_is_length_capped(self):
        self.assertEqual(len(" ".join(search_words("a" * 500))), MAX_SEARCH_LENGTH)

    def test_identity_tags_are_editable(self):
        self.session.add(SearchSettings(id=1, identity_tags_json='["fanmeet"]', typo_tolerance="normal"))
        self.session.commit()
        # "ViewMim" is no longer an identity tag, so every event's tag matches it again
        self.assertEqual(len(self.search("viewmim")), 4)
        # and the new identity tag no longer matches through tags
        self.session.add(Event(name="Other", tags_json='["fanmeet"]'))
        self.session.commit()
        self.assertEqual(self.search("fanmeet"), ["ViewMim Fan Meet Bangkok"])

    def test_typo_tolerance_levels(self):
        self.assertEqual(self.search("bangkoc"), ["ViewMim Fan Meet Bangkok"])
        self.session.add(SearchSettings(id=1, identity_tags_json="[]", typo_tolerance="off"))
        self.session.commit()
        self.assertEqual(self.search("bangkoc"), [])
        self.session.get(SearchSettings, 1).typo_tolerance = "loose"
        self.session.commit()
        self.assertEqual(self.search("bangkk"), ["ViewMim Fan Meet Bangkok"])


class ProjectSearchTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.session.add(Project(title="Love Sick", original_title="ซีรีส์", hashtag="LoveSick", category="series"))
        self.session.add(Project(title="Concert Night", hashtag="ViewMim", category="concert"))
        self.session.commit()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def search(self, term):
        clause = project_search_filter(self.session, term)
        return sorted(self.session.exec(select(Project.title).where(clause)).all())

    def test_matches_title_hashtag_and_category_with_typos(self):
        self.assertEqual(self.search("love"), ["Love Sick"])
        self.assertEqual(self.search("lovesick"), ["Love Sick"])
        self.assertEqual(self.search("concrt"), ["Concert Night"])
        self.assertEqual(self.search("series"), ["Love Sick"])

    def test_identity_hashtag_does_not_match_projects(self):
        self.assertEqual(self.search("viewmim"), [])


class SearchSettingsApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_defaults_then_update(self):
        defaults = get_search_settings(self.session)
        self.assertIn("ViewMim", defaults["identity_tags"])
        self.assertEqual(defaults["typo_tolerance"], "normal")

        saved = update_search_settings(
            SearchSettingsPayload(identity_tags=["#Foo", " foo ", "", "Bar"], typo_tolerance="loose"), self.session,
        )
        self.assertEqual(saved["identity_tags"], ["Foo", "Bar"])
        self.assertEqual(get_search_settings(self.session)["typo_tolerance"], "loose")

    def test_empty_list_is_allowed_and_invalid_input_rejected(self):
        saved = update_search_settings(SearchSettingsPayload(identity_tags=[], typo_tolerance="off"), self.session)
        self.assertEqual(saved["identity_tags"], [])
        with self.assertRaises(HTTPException):
            update_search_settings(SearchSettingsPayload(identity_tags=[], typo_tolerance="wild"), self.session)
        with self.assertRaises(HTTPException):
            update_search_settings(SearchSettingsPayload(identity_tags=["x" * 61], typo_tolerance="off"), self.session)


class AdminEventCountFilterTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        view = Author(name="View")
        mim = Author(name="Mim")
        self.session.add_all([view, mim])
        self.session.commit()
        early = Event(name="Early", category="show", start_date="2025-01-01", event_date="2025-01-01", dates_json='["2025-01-01"]')
        late = Event(name="Late", category="live", start_date="2025-06-01", event_date="2025-06-01", dates_json='["2025-06-01"]')
        self.session.add_all([early, late])
        self.session.commit()
        self.session.add(EventAuthorLink(event_id=early.id, author_id=view.id))
        self.session.add(EventAuthorLink(event_id=late.id, author_id=mim.id))
        self.session.commit()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def count(self, **filters):
        args = {"name": None, "category": None, "subcategory": None, "author": None, "visible_start": None, "visible_end": None}
        args.update(filters)
        return count_admin_events(session=self.session, **args)["count"]

    def test_count_matches_the_list_filters(self):
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.count(author="view"), 1)
        self.assertEqual(self.count(author="viewmim"), 0)
        self.assertEqual(self.count(category="live"), 1)
        self.assertEqual(self.count(visible_start="2025-03-01"), 1)
        self.assertEqual(self.count(visible_end="2025-03-01"), 1)
        self.assertEqual(self.count(visible_start="2025-03-01", visible_end="2025-12-31", author="mim"), 1)


if __name__ == "__main__":
    unittest.main()
