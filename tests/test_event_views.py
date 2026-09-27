import unittest

from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine

from routers.event_views import (
    EventViewCreate,
    EventViewUpdate,
    create_event_view,
    delete_event_view,
    get_event_view,
    list_admin_event_views,
    list_public_event_views,
    update_event_view,
)


class EventViewTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_create_resolve_update_and_delete(self):
        saved = create_event_view(
            EventViewCreate(
                title="ViewMim Fan Meets",
                slug=" ViewMim Fan Meets ",
                category="fan event",
                subcategory="fan meet",
                author="viewmim",
                event_sort="oldest",
                view_mode="calendar",
            ),
            self.session,
        )
        self.assertEqual(saved.slug, "viewmim-fan-meets")
        self.assertEqual(get_event_view(saved.slug, self.session).author, "viewmim")
        self.assertEqual(len(list_admin_event_views(self.session)), 1)

        updated = update_event_view(
            saved.id,
            EventViewUpdate(slug="fan-meets", name_filter="#tag", view_mode="list"),
            self.session,
        )
        self.assertEqual(updated.slug, "fan-meets")
        self.assertEqual(updated.name_filter, "#tag")
        self.assertEqual(updated.view_mode, "list")

        delete_event_view(saved.id, self.session)
        with self.assertRaises(HTTPException) as missing:
            get_event_view("fan-meets", self.session)
        self.assertEqual(missing.exception.status_code, 404)

    def test_hidden_and_duplicate_slugs_are_rejected(self):
        saved = create_event_view(
            EventViewCreate(title="Hidden", slug="same", is_visible=False),
            self.session,
        )
        with self.assertRaises(HTTPException) as hidden:
            get_event_view(saved.slug, self.session)
        self.assertEqual(hidden.exception.status_code, 404)

        with self.assertRaises(HTTPException) as duplicate:
            create_event_view(EventViewCreate(title="Duplicate", slug="same"), self.session)
        self.assertEqual(duplicate.exception.status_code, 400)
        self.assertEqual(list_public_event_views(self.session), [])

    def test_invalid_filter_combination_is_rejected(self):
        with self.assertRaises(HTTPException) as invalid:
            create_event_view(
                EventViewCreate(title="Bad", slug="bad", category="live", subcategory="fan meet"),
                self.session,
            )
        self.assertEqual(invalid.exception.status_code, 400)

    def test_saved_order_controls_admin_and_public_lists(self):
        first = create_event_view(EventViewCreate(title="First", slug="first"), self.session)
        second = create_event_view(EventViewCreate(title="Second", slug="second"), self.session)
        self.assertEqual([view.slug for view in list_public_event_views(self.session)], ["first", "second"])

        update_event_view(second.id, EventViewUpdate(sort_order=-1), self.session)

        self.assertEqual([view.slug for view in list_admin_event_views(self.session)], ["second", "first"])
        self.assertEqual([view.slug for view in list_public_event_views(self.session)], ["second", "first"])


if __name__ == "__main__":
    unittest.main()
