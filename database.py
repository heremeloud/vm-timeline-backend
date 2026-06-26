from sqlmodel import SQLModel, create_engine, Session
from sqlalchemy import text

DATABASE_URL = "sqlite:///vm-social.db"

engine = create_engine(
    DATABASE_URL,
    echo=True
)

def create_db_and_tables():
    SQLModel.metadata.create_all(engine)

def run_migrations():
    """Add any new columns that may not exist yet (safe to run multiple times)."""
    with engine.connect() as conn:
        # ── author table ────────────────────────────────────────
        result = conn.execute(text("PRAGMA table_info(author)"))
        author_cols = {row[1] for row in result}

        if "twitter_pfp_url" not in author_cols:
            conn.execute(text("ALTER TABLE author ADD COLUMN twitter_pfp_url VARCHAR"))
            conn.commit()
            print("Migration: added twitter_pfp_url to author")

        if "ig_pfp_url" not in author_cols:
            conn.execute(text("ALTER TABLE author ADD COLUMN ig_pfp_url VARCHAR"))
            conn.commit()
            print("Migration: added ig_pfp_url to author")

        if "tiktok_pfp_url" not in author_cols:
            conn.execute(text("ALTER TABLE author ADD COLUMN tiktok_pfp_url VARCHAR"))
            conn.commit()
            print("Migration: added tiktok_pfp_url to author")

        # ── post table ──────────────────────────────────────────
        result = conn.execute(text("PRAGMA table_info(post)"))
        post_cols = {row[1] for row in result}

        if "media_urls_json" not in post_cols:
            conn.execute(text("ALTER TABLE post ADD COLUMN media_urls_json VARCHAR DEFAULT '[]'"))
            conn.commit()
            print("Migration: added media_urls_json to post")

        if "caption_translation_note" not in post_cols:
            conn.execute(text("ALTER TABLE post ADD COLUMN caption_translation_note VARCHAR"))
            conn.commit()
            print("Migration: added caption_translation_note to post")

        if "is_visible" not in post_cols:
            conn.execute(text("ALTER TABLE post ADD COLUMN is_visible BOOLEAN DEFAULT 1"))
            conn.execute(text("UPDATE post SET is_visible = 1 WHERE is_visible IS NULL"))
            conn.commit()
            print("Migration: added is_visible to post")

        # ── posttext table ──────────────────────────────────────
        result = conn.execute(text("PRAGMA table_info(posttext)"))
        posttext_cols = {row[1] for row in result}

        if "note" not in posttext_cols:
            conn.execute(text("ALTER TABLE posttext ADD COLUMN note VARCHAR"))
            conn.commit()
            print("Migration: added note to posttext")

        # ── topicitem table ─────────────────────────────────────
        result = conn.execute(text("PRAGMA table_info(topicitem)"))
        topicitem_cols = {row[1] for row in result}

        if topicitem_cols and "show_replies" not in topicitem_cols:
            conn.execute(text("ALTER TABLE topicitem ADD COLUMN show_replies BOOLEAN DEFAULT 1"))
            conn.execute(text("UPDATE topicitem SET show_replies = 1 WHERE show_replies IS NULL"))
            conn.commit()
            print("Migration: added show_replies to topicitem")

        if topicitem_cols and "media_index" not in topicitem_cols:
            conn.execute(text("ALTER TABLE topicitem ADD COLUMN media_index INTEGER"))
            conn.commit()
            print("Migration: added media_index to topicitem")

        if topicitem_cols and "media_indices_json" not in topicitem_cols:
            conn.execute(text("ALTER TABLE topicitem ADD COLUMN media_indices_json VARCHAR DEFAULT '[]'"))
            conn.execute(text("UPDATE topicitem SET media_indices_json = '[' || media_index || ']' WHERE media_index IS NOT NULL"))
            conn.commit()
            print("Migration: added media_indices_json to topicitem")

        # ── event table ─────────────────────────────────────────
        result = conn.execute(text("PRAGMA table_info(event)"))
        event_cols = {row[1] for row in result}

        if "project_id" not in event_cols:
            conn.execute(text("ALTER TABLE event ADD COLUMN project_id INTEGER REFERENCES project(id)"))
            conn.commit()
            print("Migration: added project_id to event")

        if "parent_event_id" not in event_cols:
            conn.execute(text("ALTER TABLE event ADD COLUMN parent_event_id INTEGER REFERENCES event(id)"))
            conn.commit()
            print("Migration: added parent_event_id to event")

        if "is_visible" not in event_cols:
            conn.execute(text("ALTER TABLE event ADD COLUMN is_visible BOOLEAN DEFAULT 1"))
            conn.execute(text("UPDATE event SET is_visible = 1 WHERE is_visible IS NULL"))
            conn.commit()
            print("Migration: added is_visible to event")

        if "start_date" not in event_cols:
            conn.execute(text("ALTER TABLE event ADD COLUMN start_date VARCHAR"))
            conn.execute(text("UPDATE event SET start_date = event_date WHERE start_date IS NULL AND event_date IS NOT NULL"))
            conn.commit()
            print("Migration: added start_date to event")
        elif "event_date" in event_cols:
            missing_start_dates = conn.execute(
                text("SELECT COUNT(*) FROM event WHERE start_date IS NULL AND event_date IS NOT NULL")
            ).scalar_one()
            if missing_start_dates:
                conn.execute(text("UPDATE event SET start_date = event_date WHERE start_date IS NULL AND event_date IS NOT NULL"))
                conn.commit()

        if "end_date" not in event_cols:
            conn.execute(text("ALTER TABLE event ADD COLUMN end_date VARCHAR"))
            conn.commit()
            print("Migration: added end_date to event")

        # ── project table ────────────────────────────────────────
        result = conn.execute(text("PRAGMA table_info(project)"))
        project_cols = {row[1] for row in result}

        if "start_date" not in project_cols:
            conn.execute(text("ALTER TABLE project ADD COLUMN start_date VARCHAR"))
            conn.commit()
            print("Migration: added start_date to project")

        if "end_date" not in project_cols:
            conn.execute(text("ALTER TABLE project ADD COLUMN end_date VARCHAR"))
            conn.commit()
            print("Migration: added end_date to project")

        if "original_title" not in project_cols:
            conn.execute(text("ALTER TABLE project ADD COLUMN original_title VARCHAR"))
            conn.commit()
            print("Migration: added original_title to project")

        if "slug" not in project_cols:
            conn.execute(text("ALTER TABLE project ADD COLUMN slug VARCHAR"))
            conn.commit()
            print("Migration: added slug to project")

        if "playlists_json" not in project_cols:
            conn.execute(text("ALTER TABLE project ADD COLUMN playlists_json VARCHAR DEFAULT '[]'"))
            conn.commit()
            print("Migration: added playlists_json to project")

        if "announcement_url" not in project_cols:
            conn.execute(text("ALTER TABLE project ADD COLUMN announcement_url VARCHAR"))
            conn.commit()
            print("Migration: added announcement_url to project")

        if "tweet_url" not in project_cols:
            conn.execute(text("ALTER TABLE project ADD COLUMN tweet_url VARCHAR"))
            conn.commit()
            print("Migration: added tweet_url to project")

        if "mydramalist_url" not in project_cols:
            conn.execute(text("ALTER TABLE project ADD COLUMN mydramalist_url VARCHAR"))
            conn.commit()
            print("Migration: added mydramalist_url to project")

        if "gmmtv_url" not in project_cols:
            conn.execute(text("ALTER TABLE project ADD COLUMN gmmtv_url VARCHAR"))
            conn.commit()
            print("Migration: added gmmtv_url to project")

        if "youtube_url" not in project_cols:
            conn.execute(text("ALTER TABLE project ADD COLUMN youtube_url VARCHAR"))
            conn.commit()
            print("Migration: added youtube_url to project")

        if "spotify_url" not in project_cols:
            conn.execute(text("ALTER TABLE project ADD COLUMN spotify_url VARCHAR"))
            conn.commit()
            print("Migration: added spotify_url to project")

        if "apple_music_url" not in project_cols:
            conn.execute(text("ALTER TABLE project ADD COLUMN apple_music_url VARCHAR"))
            conn.commit()
            print("Migration: added apple_music_url to project")

        if "parent_project_id" not in project_cols:
            conn.execute(text("ALTER TABLE project ADD COLUMN parent_project_id INTEGER REFERENCES project(id)"))
            conn.commit()
            print("Migration: added parent_project_id to project")

        if "is_visible" not in project_cols:
            conn.execute(text("ALTER TABLE project ADD COLUMN is_visible BOOLEAN DEFAULT 1"))
            conn.execute(text("UPDATE project SET is_visible = 1 WHERE is_visible IS NULL"))
            conn.commit()
            print("Migration: added is_visible to project")

        # ── author table ─────────────────────────────────────────
        result = conn.execute(text("PRAGMA table_info(author)"))
        author_cols = {row[1] for row in result}

        if "full_name" not in author_cols:
            conn.execute(text("ALTER TABLE author ADD COLUMN full_name VARCHAR"))
            conn.commit()
            print("Migration: added full_name to author")

        if "birthday" not in author_cols:
            conn.execute(text("ALTER TABLE author ADD COLUMN birthday VARCHAR"))
            conn.commit()
            print("Migration: added birthday to author")

        if "twitter_url" not in author_cols:
            conn.execute(text("ALTER TABLE author ADD COLUMN twitter_url VARCHAR"))
            conn.commit()
            print("Migration: added twitter_url to author")

        if "instagram_url" not in author_cols:
            conn.execute(text("ALTER TABLE author ADD COLUMN instagram_url VARCHAR"))
            conn.commit()
            print("Migration: added instagram_url to author")

        if "tiktok_url" not in author_cols:
            conn.execute(text("ALTER TABLE author ADD COLUMN tiktok_url VARCHAR"))
            conn.commit()
            print("Migration: added tiktok_url to author")

        if "gmmtv_url" not in author_cols:
            conn.execute(text("ALTER TABLE author ADD COLUMN gmmtv_url VARCHAR"))
            conn.commit()
            print("Migration: added gmmtv_url to author")

        if "mydramalist_url" not in author_cols:
            conn.execute(text("ALTER TABLE author ADD COLUMN mydramalist_url VARCHAR"))
            conn.commit()
            print("Migration: added mydramalist_url to author")

        if "fc_url" not in author_cols:
            conn.execute(text("ALTER TABLE author ADD COLUMN fc_url VARCHAR"))
            conn.commit()
            print("Migration: added fc_url to author")

        if "show_on_timeline" not in author_cols:
            conn.execute(text("ALTER TABLE author ADD COLUMN show_on_timeline BOOLEAN DEFAULT 1"))
            conn.execute(text("UPDATE author SET show_on_timeline = 1 WHERE show_on_timeline IS NULL"))
            conn.commit()
            print("Migration: added show_on_timeline to author")

        # ── topic table ─────────────────────────────────────────
        result = conn.execute(text("PRAGMA table_info(topic)"))
        topic_cols = {row[1] for row in result}

        if topic_cols and "sort_order" not in topic_cols:
            conn.execute(text("ALTER TABLE topic ADD COLUMN sort_order INTEGER DEFAULT 0"))
            conn.execute(text("UPDATE topic SET sort_order = id WHERE sort_order IS NULL OR sort_order = 0"))
            conn.commit()
            print("Migration: added sort_order to topic")

        if topic_cols and "start_date" not in topic_cols:
            conn.execute(text("ALTER TABLE topic ADD COLUMN start_date VARCHAR"))
            conn.commit()
            print("Migration: added start_date to topic")

        if topic_cols and "end_date" not in topic_cols:
            conn.execute(text("ALTER TABLE topic ADD COLUMN end_date VARCHAR"))
            conn.commit()
            print("Migration: added end_date to topic")

        if topic_cols and "is_visible" not in topic_cols:
            conn.execute(text("ALTER TABLE topic ADD COLUMN is_visible BOOLEAN DEFAULT 1"))
            conn.execute(text("UPDATE topic SET is_visible = 1 WHERE is_visible IS NULL"))
            conn.commit()
            print("Migration: added is_visible to topic")

def get_session():
    with Session(engine) as session:
        yield session
