import unittest
from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine
from models import Event
from routers.events import EventCreate, EventUpdate, create_event, update_event, list_events, list_admin_events, get_admin_event, get_event


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

    def test_date_specific_keyword_and_hashtag_roundtrip(self):
        date_items = [
            {"date": "2026-10-10", "keyword": "Second show", "hashtag": "#SecondShow"},
            {"date": "2026-09-01", "keyword": "First show", "hashtag": "FirstShow"},
        ]
        saved = create_event(EventCreate(name="Two shows", date_items=date_items), self.session)
        self.assertEqual(saved["dates"], ["2026-09-01", "2026-10-10"])
        self.assertEqual(saved["date_items"], [
            {"date": "2026-09-01", "keyword": "First show", "hashtag": "FirstShow"},
            {"date": "2026-10-10", "keyword": "Second show", "hashtag": "SecondShow"},
        ])
        self.assertEqual(len(list_events(name="SecondShow", session=self.session)), 1)
        self.assertEqual(len(list_events(keyword="First show", session=self.session)), 1)
        self.assertEqual(len(list_events(tag="SecondShow", session=self.session)), 1)

        saved = update_event(saved["id"], EventUpdate(date_items=[
            {"date": "2026-11-02", "keyword": "Final show", "hashtag": "FinalShow"},
        ]), self.session)
        self.assertEqual(saved["dates"], ["2026-11-02"])
        self.assertEqual(saved["date_items"][0]["keyword"], "Final show")

    def test_live_media_display_type_roundtrip_and_validation(self):
        from pydantic import ValidationError

        media = [
            {"url": "https://example.com/interview", "date": "2026-09-01", "display_type": "article"},
            {"url": "https://x.com/example/status/123", "display_type": "tweet", "hashtag": "#Interview"},
        ]
        saved = create_event(EventCreate(name="Interview", live_media_items=media), self.session)
        self.assertEqual(saved["live_media_items"], [
            {"url": "https://example.com/interview", "date": "2026-09-01", "keyword": None, "hashtag": None, "display_type": "article"},
            {"url": "https://x.com/example/status/123", "date": None, "keyword": None, "hashtag": "Interview", "display_type": "tweet"},
        ])

        legacy = create_event(EventCreate(
            name="Legacy media",
            live_media_items=[{"url": "https://example.com/video"}],
        ), self.session)
        self.assertEqual(legacy["live_media_items"][0]["display_type"], "auto")
        self.assertIsNone(legacy["live_media_items"][0]["date"])

        with self.assertRaises(ValidationError):
            EventCreate(
                name="Invalid media display",
                live_media_items=[{"url": "https://example.com", "display_type": "iframe"}],
            )

    def test_public_announcement_is_exposed_without_private_announcement_list(self):
        tweet_url = "https://x.com/example/status/123"
        saved = create_event(EventCreate(
            name="Interview announcement",
            announcement_urls=[tweet_url, "https://example.com/private-reference"],
            public_announcement_url=tweet_url,
        ), self.session)
        self.assertEqual(saved["public_announcement_url"], tweet_url)
        self.assertNotIn("announcement_urls", saved)

        public_event = list_events(session=self.session)[0]
        self.assertEqual(public_event["public_announcement_url"], tweet_url)
        self.assertNotIn("announcement_urls", public_event)

        admin_event = get_admin_event(saved["id"], session=self.session)["event"]
        self.assertEqual(admin_event["announcement_urls"], [tweet_url, "https://example.com/private-reference"])

    def test_interview_content_is_private_until_enabled(self):
        content = "Question one\n\nAnswer one"
        saved = create_event(EventCreate(
            name="Private interview",
            category="show",
            subcategory="interview",
            interview_content=content,
        ), self.session)
        self.assertNotIn("interview_content", saved)
        self.assertFalse(saved["show_interview_content"])

        admin_event = get_admin_event(saved["id"], session=self.session)["event"]
        self.assertEqual(admin_event["interview_content"], content)

        update_event(saved["id"], EventUpdate(show_interview_content=True), self.session)
        public_event = get_event(saved["id"], session=self.session)["event"]
        self.assertEqual(public_event["interview_content"], content)
        self.assertTrue(public_event["show_interview_content"])
        self.assertNotIn("interview_content", list_events(session=self.session)[0])

    def test_migration_preserves_legacy_event(self):
        from unittest.mock import patch
        from sqlalchemy import text
        import database
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO event (name, tags_json, is_visible, show_interview_content, live_urls, live_media_items_json, announcement_urls_json, dates_json, date_items_json, media_urls_json, photo_items_json, media_url) VALUES ('Old', '[]', 1, 0, '', '[]', '[]', '[]', '[]', '[]', '[]', 'old.jpg')"))
            conn.execute(text("ALTER TABLE event DROP COLUMN photo_items_json"))
            conn.execute(text("ALTER TABLE event DROP COLUMN dates_json"))
            conn.execute(text("ALTER TABLE event DROP COLUMN date_items_json"))
            conn.execute(text("ALTER TABLE event DROP COLUMN media_urls_json"))
            conn.execute(text("ALTER TABLE event DROP COLUMN public_announcement_url"))
            conn.execute(text("ALTER TABLE event DROP COLUMN interview_content"))
            conn.execute(text("ALTER TABLE event DROP COLUMN show_interview_content"))
            conn.execute(text("DROP INDEX ix_eventcategoryoption_is_default"))
            conn.execute(text("ALTER TABLE eventcategoryoption DROP COLUMN is_default"))
        with patch.object(database, "engine", self.engine):
            database.run_migrations()
            database.run_migrations()
        saved = list_events(session=self.session)[0]
        self.assertEqual(saved['media_urls'], ['old.jpg'])
        self.assertEqual(saved['dates'], [])
        self.assertEqual(saved['date_items'], [])
        self.assertIsNone(saved['public_announcement_url'])
        self.assertNotIn('interview_content', saved)
        self.assertFalse(saved['show_interview_content'])
        with self.engine.connect() as conn:
            default_count = conn.execute(text(
                "SELECT COUNT(*) FROM eventcategoryoption WHERE is_default = 1"
            )).scalar_one()
        self.assertEqual(default_count, 1)

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
