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
        # ── post table ──────────────────────────────────────────
        result = conn.execute(text("PRAGMA table_info(post)"))
        post_cols = {row[1] for row in result}

        if "media_urls_json" not in post_cols:
            conn.execute(text("ALTER TABLE post ADD COLUMN media_urls_json VARCHAR DEFAULT '[]'"))
            conn.commit()
            print("Migration: added media_urls_json to post")

        # ── event table ─────────────────────────────────────────
        result = conn.execute(text("PRAGMA table_info(event)"))
        event_cols = {row[1] for row in result}

        if "project_id" not in event_cols:
            conn.execute(text("ALTER TABLE event ADD COLUMN project_id INTEGER REFERENCES project(id)"))
            conn.commit()
            print("Migration: added project_id to event")

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

def get_session():
    with Session(engine) as session:
        yield session
