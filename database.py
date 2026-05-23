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
        # Check existing columns
        result = conn.execute(text("PRAGMA table_info(post)"))
        existing = {row[1] for row in result}

        if "media_urls_json" not in existing:
            conn.execute(text("ALTER TABLE post ADD COLUMN media_urls_json VARCHAR DEFAULT '[]'"))
            conn.commit()
            print("Migration: added media_urls_json column to post table")

def get_session():
    with Session(engine) as session:
        yield session
