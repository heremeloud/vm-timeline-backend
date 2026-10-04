import os
import tempfile
import unittest

from sqlalchemy import text
from sqlmodel import SQLModel, create_engine

import database
from models import Event, EventCategoryOption


class LegacyCategoryMigrationTests(unittest.TestCase):
    """run_migrations runs at every backend start, so it must not undo categories the admin has configured."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.original_engine = database.engine
        database.engine = create_engine(f"sqlite:///{os.path.join(self.directory.name, 'test.db')}")
        SQLModel.metadata.create_all(database.engine)

    def tearDown(self):
        database.engine.dispose()
        database.engine = self.original_engine
        self.directory.cleanup()

    def categories_of(self, event_id):
        with database.engine.connect() as conn:
            return tuple(conn.execute(
                text("SELECT category, subcategory FROM event WHERE id = :id"), {"id": event_id}
            ).one())

    def add_event(self, **values):
        with database.Session(database.engine) as session:
            event = Event(name="Event", **values)
            session.add(event)
            session.commit()
            return event.id

    def test_an_interview_category_that_is_configured_is_left_alone_on_every_start(self):
        with database.Session(database.engine) as session:
            session.add(EventCategoryOption(name="interview", label="Interview", sort_order=0))
            session.commit()
        event_id = self.add_event(category="interview", subcategory="magazine")

        database.run_migrations()
        database.run_migrations()  # a second start

        self.assertEqual(self.categories_of(event_id), ("interview", "magazine"))

    def test_legacy_interview_events_still_become_show_interview_when_it_is_not_a_category(self):
        event_id = self.add_event(category="Interview")

        database.run_migrations()

        self.assertEqual(self.categories_of(event_id), ("show", "interview"))

    def test_legacy_fan_events_convert_only_while_those_are_not_categories(self):
        legacy = self.add_event(category="fan sign")
        database.run_migrations()
        self.assertEqual(self.categories_of(legacy), ("fan event", "fan sign"))

        with database.Session(database.engine) as session:
            session.add(EventCategoryOption(name="fan meet", label="Fan Meet", sort_order=9))
            session.commit()
        configured = self.add_event(category="fan meet")
        database.run_migrations()
        self.assertEqual(self.categories_of(configured), ("fan meet", None))


if __name__ == "__main__":
    unittest.main()
