import unittest
from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine
from models import Event
from routers.events import EventCreate, EventUpdate, create_event, update_event, list_events, list_admin_events


class EventCollectionsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_roundtrip_and_filtering(self):
        saved = create_event(EventCreate(name='Separate days', dates=['2026-10-10', '2026-09-01', '2026-09-01'], media_urls=[' https://example.com/a.jpg ', 'https://example.com/b.jpg']), self.session)
        self.assertEqual(saved['dates'], ['2026-09-01', '2026-10-10'])
        self.assertEqual(len(saved['media_urls']), 2)
        self.assertEqual(saved['media_url'], saved['media_urls'][0])
        for listing in (list_events, list_admin_events):
            self.assertEqual(listing(visible_start='2026-09-15', visible_end='2026-09-16', session=self.session), [])
            self.assertEqual(len(listing(visible_start='2026-10-10', visible_end='2026-10-10', session=self.session)), 1)
        saved = update_event(saved['id'], EventUpdate(name='Renamed'), self.session)
        self.assertEqual(len(saved['dates']), 2)
        self.assertEqual(len(saved['media_urls']), 2)
        saved = update_event(saved['id'], EventUpdate(dates=['2026-11-02'], media_urls=['https://example.com/c.jpg']), self.session)
        self.assertEqual(saved['start_date'], '2026-11-02')
        self.assertEqual(saved['media_url'], 'https://example.com/c.jpg')
        saved = update_event(saved['id'], EventUpdate(dates=[], start_date='2026-09-01', end_date='2026-09-30', media_urls=[]), self.session)
        self.assertEqual(saved['dates'], [])
        self.assertEqual(saved['media_urls'], [])
        self.assertIsNone(saved['media_url'])
        self.assertEqual(len(list_events(visible_start='2026-09-15', visible_end='2026-09-16', session=self.session)), 1)

    def test_migration_preserves_legacy_event(self):
        from unittest.mock import patch
        from sqlalchemy import text
        import database
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO event (name, tags_json, is_visible, live_urls, live_media_items_json, announcement_urls_json, dates_json, media_urls_json, photo_items_json, media_url) VALUES ('Old', '[]', 1, '', '[]', '[]', '[]', '[]', '[]', 'old.jpg')"))
            conn.execute(text("ALTER TABLE event DROP COLUMN photo_items_json"))
            conn.execute(text("ALTER TABLE event DROP COLUMN dates_json"))
            conn.execute(text("ALTER TABLE event DROP COLUMN media_urls_json"))
        with patch.object(database, "engine", self.engine):
            database.run_migrations()
            database.run_migrations()
        saved = list_events(session=self.session)[0]
        self.assertEqual(saved['media_urls'], ['old.jpg'])
        self.assertEqual(saved['dates'], [])

    def test_legacy_and_invalid_date(self):
        legacy = Event(name='Legacy', media_url='https://example.com/old.jpg', start_date='2026-09-01')
        self.session.add(legacy)
        self.session.commit()
        saved = list_events(session=self.session)[0]
        self.assertEqual(saved['media_urls'], [legacy.media_url])
        self.assertEqual(saved['dates'], [])
        with self.assertRaises(HTTPException):
            create_event(EventCreate(name='Bad date', dates=['2026-02-30']), self.session)

    def test_independent_focus_roundtrip_reorder_and_clear(self):
        photos = [{"url": "a.jpg", "focal_x": 0, "focal_y": 20},
                  {"url": "b.jpg", "focal_x": 90, "focal_y": 100}]
        saved = create_event(EventCreate(name="Photos", photo_items=photos), self.session)
        self.assertEqual(saved["photo_items"], photos)
        self.assertEqual(saved["media_urls"], ["a.jpg", "b.jpg"])
        self.session.expire_all()
        self.assertEqual(list_events(session=self.session)[0]["photo_items"], photos)
        saved = update_event(saved["id"], EventUpdate(media_urls=["b.jpg", "a.jpg"]), self.session)
        self.assertEqual(saved["photo_items"], photos[::-1])
        saved = update_event(saved["id"], EventUpdate(name="Keep focus"), self.session)
        self.assertEqual(saved["photo_items"], photos[::-1])
        saved = update_event(saved["id"], EventUpdate(photo_items=[photos[0]]), self.session)
        self.assertEqual(saved["photo_items"], photos[:1])
        saved = update_event(saved["id"], EventUpdate(photo_items=[]), self.session)
        self.assertEqual(saved["photo_items"], [])
        self.assertEqual(saved["media_urls"], [])
        self.assertIsNone(saved["media_url"])

    def test_legacy_focus_and_validation(self):
        from pydantic import ValidationError
        saved = create_event(EventCreate(name="Legacy focus", media_url="a.jpg", media_focal_x=0, media_focal_y=80), self.session)
        self.assertEqual(saved["photo_items"], [{"url": "a.jpg", "focal_x": 0, "focal_y": 80}])
        with self.assertRaises(ValidationError):
            EventCreate(name="Bad focus", photo_items=[{"url": "a.jpg", "focal_x": 101}])

    def test_photo_date_assignments_survive_save_and_edit(self):
        photos = [
            {"url": "day-one.jpg", "date": "2026-09-01", "focal_x": 10, "focal_y": 20},
            {"url": "day-two.jpg", "date": "2026-09-02", "focal_x": 80, "focal_y": 90},
            {"url": "extra.jpg", "date": "2026-09-02", "focal_x": 50, "focal_y": 50},
        ]
        saved = create_event(EventCreate(name="Dated photos", dates=['2026-09-01', '2026-09-02'], photo_items=photos), self.session)
        self.session.expire_all()
        self.assertEqual(list_events(session=self.session)[0]['photo_items'], photos)
        photos[0]['date'] = '2026-09-02'
        saved = update_event(saved['id'], EventUpdate(photo_items=photos), self.session)
        self.assertEqual(saved['photo_items'], photos)
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            EventCreate(name='Bad date', photo_items=[{'url': 'a.jpg', 'date': '2026-02-30'}])
