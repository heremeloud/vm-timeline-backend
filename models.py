from sqlmodel import SQLModel, Field, Relationship
from typing import Optional, List


# ---------- LINK TABLE: Project <-> Author (many-to-many) ----------
class ProjectAuthorLink(SQLModel, table=True):
    project_id: Optional[int] = Field(
        default=None, foreign_key="project.id", primary_key=True
    )
    author_id: Optional[int] = Field(
        default=None, foreign_key="author.id", primary_key=True
    )


# ---------- LINK TABLE: Event <-> Author (many-to-many) ----------
class EventAuthorLink(SQLModel, table=True):
    event_id: Optional[int] = Field(
        default=None, foreign_key="event.id", primary_key=True
    )
    author_id: Optional[int] = Field(
        default=None, foreign_key="author.id", primary_key=True
    )


# ============================================================
# AUTHOR TABLE
# ============================================================
class Author(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    name: str = Field(index=True, unique=True)
    full_name: Optional[str] = None          # full legal/stage name
    profile_photo_url: Optional[str] = None
    ig_pfp_url: Optional[str] = None
    twitter_pfp_url: Optional[str] = None
    tiktok_pfp_url: Optional[str] = None
    birthday: Optional[str] = None           # e.g. "2000-03-15"
    twitter_url: Optional[str] = None
    instagram_url: Optional[str] = None
    tiktok_url: Optional[str] = None
    gmmtv_url: Optional[str] = None
    mydramalist_url: Optional[str] = None
    fc_url: Optional[str] = None             # official fan club URL
    show_on_timeline: bool = Field(default=False)

    # Relationships
    posts: List["Post"] = Relationship(back_populates="author_obj")
    texts: List["PostText"] = Relationship(back_populates="author_obj")

    # events this author participates in
    events: List["Event"] = Relationship(
        back_populates="authors",
        link_model=EventAuthorLink,
    )

    # projects this author participates in
    projects: List["Project"] = Relationship(
        back_populates="authors",
        link_model=ProjectAuthorLink,
    )


# ============================================================
# POST — Main IG/Twitter posts & tweet replies
# ============================================================
class Post(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    platform: str  # "instagram", "x", "tt" etc.
    external_url: str
    external_id: str

    author_id: Optional[int] = Field(default=None, foreign_key="author.id")

    caption: Optional[str] = None
    caption_translation: Optional[str] = None
    caption_translation_note: Optional[str] = None   # optional translator's note

    posted_at: Optional[str] = None
    media_url: Optional[str] = None

    media_urls_json: str = Field(default="[]")  # JSON array of media URLs for story carousels
    is_visible: bool = Field(default=True)
    is_adult: bool = Field(default=False)

    parent_id: Optional[int] = Field(default=None, foreign_key="post.id")

    children: List["Post"] = Relationship(
        back_populates="parent",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )

    parent: Optional["Post"] = Relationship(
        back_populates="children",
        sa_relationship_kwargs={"remote_side": "Post.id"},
    )

    texts: List["PostText"] = Relationship(
        back_populates="post",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )

    author_obj: Optional[Author] = Relationship(back_populates="posts")

    @property
    def author_name(self):
        return self.author_obj.name if self.author_obj else None

    @property
    def author_photo(self):
        return self.author_obj.profile_photo_url if self.author_obj else None

    @property
    def author_ig_pfp_url(self):
        return self.author_obj.ig_pfp_url if self.author_obj else None

    @property
    def author_twitter_pfp_url(self):
        return self.author_obj.twitter_pfp_url if self.author_obj else None

    @property
    def author_tiktok_pfp_url(self):
        return self.author_obj.tiktok_pfp_url if self.author_obj else None


# ============================================================
# POST TEXT — IG replies, translations, general text entities
# ============================================================
class PostText(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    post_id: int = Field(foreign_key="post.id")

    type: str       # "ig-reply", "ig-translation", "tt-reply", ...
    language: str   # "th", "en", etc.

    author_id: Optional[int] = Field(default=None, foreign_key="author.id")

    content: Optional[str] = None
    translation: Optional[str] = None   # optional translation of the reply
    posted_at: Optional[str] = None
    media_url: Optional[str] = None
    note: Optional[str] = None     # optional translator's note

    parent_comment_id: Optional[int] = Field(
        default=None, foreign_key="posttext.id"
    )

    post: Optional[Post] = Relationship(back_populates="texts")
    author_obj: Optional[Author] = Relationship(back_populates="texts")

    @property
    def author_name(self):
        return self.author_obj.name if self.author_obj else None

    @property
    def author_photo(self):
        return self.author_obj.profile_photo_url if self.author_obj else None

    @property
    def author_ig_pfp_url(self):
        return self.author_obj.ig_pfp_url if self.author_obj else None

    @property
    def author_twitter_pfp_url(self):
        return self.author_obj.twitter_pfp_url if self.author_obj else None

    @property
    def author_tiktok_pfp_url(self):
        return self.author_obj.tiktok_pfp_url if self.author_obj else None


# ============================================================
# TOPIC / SPECIAL — private curated mini timelines
# ============================================================
class Topic(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    title: str = Field(index=True)
    original_title: Optional[str] = None
    slug: Optional[str] = Field(default=None, index=True, unique=True)
    description: Optional[str] = None
    cover_url: Optional[str] = None
    is_public: bool = Field(default=False)
    is_visible: bool = Field(default=True)
    created_at: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    sort_order: int = Field(default=0, index=True)

    items: List["TopicItem"] = Relationship(
        back_populates="topic",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class TopicItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    topic_id: int = Field(foreign_key="topic.id")
    post_id: int = Field(foreign_key="post.id")

    happened_at: Optional[str] = Field(default=None, index=True)  # YYYY-MM-DDTHH:mm
    label: Optional[str] = None
    note: Optional[str] = None
    show_replies: bool = Field(default=True)
    media_index: Optional[int] = None
    media_indices_json: str = Field(default="[]")
    sort_order: int = Field(default=0)

    topic: Optional[Topic] = Relationship(back_populates="items")
    post: Optional[Post] = Relationship()


# ============================================================
# PROJECT TABLE — series, concerts, movies, variety shows
# ============================================================
class Project(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    title: str = Field(index=True)
    original_title: Optional[str] = None          # Thai title
    slug: Optional[str] = Field(default=None, index=True, unique=True)
    category: Optional[str] = Field(default=None, index=True)  # series, concert, movie, variety
    thumbnail_url: Optional[str] = None
    is_visible: bool = Field(default=True)
    year: Optional[int] = None
    description: Optional[str] = None
    parent_project_id: Optional[int] = Field(default=None, foreign_key="project.id")
    playlist_id: Optional[str] = None             # legacy single playlist ID (kept for compat)
    playlists_json: str = Field(default="[]")     # JSON array of YouTube playlist IDs
    announcement_url: Optional[str] = None        # official announcement link
    tweet_url: Optional[str] = None               # tweet with media (teaser, promo, etc.)
    youtube_url: Optional[str] = None             # single YouTube video URL
    mydramalist_url: Optional[str] = None         # MyDramaList page URL
    gmmtv_url: Optional[str] = None              # GMMTV official site URL
    spotify_url: Optional[str] = None             # Spotify album/track URL
    apple_music_url: Optional[str] = None         # Apple Music album/track URL
    start_date: Optional[str] = None              # YYYY-MM-DD
    end_date: Optional[str] = None                # YYYY-MM-DD (optional, for ranges)

    authors: List[Author] = Relationship(
        back_populates="projects",
        link_model=ProjectAuthorLink,
    )

    events: List["Event"] = Relationship(back_populates="project")


# ---------- EVENT TABLE ----------
class Event(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    name: str = Field(index=True)                  # required
    location: Optional[str] = None                 # optional
    keyword: Optional[str] = Field(default=None, index=True)

    category: Optional[str] = Field(default=None, index=True)  # e.g. program, live, interview, event, fan meet
    tags_json: str = Field(default="[]")           # list[str] stored as JSON
    media_url: Optional[str] = None                # one image url
    is_visible: bool = Field(default=True)
    event_date: Optional[str] = Field(default=None, index=True)
    start_date: Optional[str] = Field(default=None, index=True)
    end_date: Optional[str] = Field(default=None, index=True)
    announcement_url: Optional[str] = None
    live_urls: str = Field(default="")             # comma-separated live stream urls

    project_id: Optional[int] = Field(default=None, foreign_key="project.id")
    parent_event_id: Optional[int] = Field(default=None, foreign_key="event.id")

    # participants
    authors: List[Author] = Relationship(
        back_populates="events",
        link_model=EventAuthorLink,
    )

    project: Optional["Project"] = Relationship(back_populates="events")
