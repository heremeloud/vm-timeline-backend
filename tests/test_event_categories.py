import unittest

from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine

from models import Event, EventView
from routers.event_categories import (
    CategoryCreate,
    CategoryUpdate,
    SubcategoryCreate,
    SubcategoryUpdate,
    category_config,
    create_category,
    create_subcategory,
    delete_category,
    delete_subcategory,
    update_category,
    update_subcategory,
)


class EventCategoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_create_order_and_rename_cascade(self):
        first = create_category(CategoryCreate(name="Fan Event"), self.session)
        second = create_category(CategoryCreate(name="Show"), self.session)
        subcategory = create_subcategory(
            SubcategoryCreate(category_id=first.id, name="Fan Meet"), self.session
        )
        event = Event(name="Meet", category="fan event", subcategory="fan meet")
        event_view = EventView(title="Meets", slug="meets", category="fan event", subcategory="fan meet")
        self.session.add(event)
        self.session.add(event_view)
        self.session.commit()

        update_category(first.id, CategoryUpdate(name="fan gathering", label="Fan Gathering"), self.session)
        update_subcategory(subcategory.id, SubcategoryUpdate(name="meet and greet"), self.session)
        self.session.refresh(event)
        self.session.refresh(event_view)
        self.assertEqual((event.category, event.subcategory), ("fan gathering", "meet and greet"))
        self.assertEqual((event_view.category, event_view.subcategory), ("fan gathering", "meet and greet"))

        update_category(second.id, CategoryUpdate(sort_order=-1), self.session)
        self.assertEqual([item["value"] for item in category_config(self.session)], ["show", "fan gathering"])

    def test_in_use_options_cannot_be_deleted(self):
        category = create_category(CategoryCreate(name="Live"), self.session)
        subcategory = create_subcategory(
            SubcategoryCreate(category_id=category.id, name="Radio"), self.session
        )
        self.session.add(Event(name="Radio", category="live", subcategory="radio"))
        self.session.commit()
        with self.assertRaises(HTTPException):
            delete_subcategory(subcategory.id, self.session)
        with self.assertRaises(HTTPException):
            delete_category(category.id, self.session)

    def test_unused_options_can_be_deleted(self):
        create_category(CategoryCreate(name="Keep"), self.session)
        category = create_category(CategoryCreate(name="Other"), self.session)
        subcategory = create_subcategory(
            SubcategoryCreate(category_id=category.id, name="Misc"), self.session
        )
        delete_subcategory(subcategory.id, self.session)
        delete_category(category.id, self.session)
        self.assertEqual([item["value"] for item in category_config(self.session)], ["keep"])


if __name__ == "__main__":
    unittest.main()
