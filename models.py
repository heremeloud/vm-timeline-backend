from sqlmodel import SQLModel, Field, Relationship
from typing import Optional, List


# ============================================================
# AUTHOR TABLE
# ============================================================
class Author(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    # View / Mim / User123
    name: str = Field(index=True, unique=True)

    # link to pfp image
    profile_photo_url: Optional[str] = None

    # Relationships
    posts: List["Post"] = Relationship(back_populates="author_obj")
    texts: List["PostText"] = Relationship(back_populates="author_obj")


# ============================================================
# POST — Main IG/Twitter posts & tweet replies
# ============================================================
class Post(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    platform: str  # "instagram", "x"
    external_url: str
    external_id: str

    # REPLACES old author:str
    author_id: Optional[int] = Field(default=None, foreign_key="author.id")

    caption: Optional[str] = None
    caption_translation: Optional[str] = None

    posted_at: Optional[str] = None
    media_url: Optional[str] = None

    # threading (for tweet reply chains or IG albums)
    parent_id: Optional[int] = Field(default=None, foreign_key="post.id")

    children: List["Post"] = Relationship(
        back_populates="parent",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )

    parent: Optional["Post"] = Relationship(
        back_populates="children",
        sa_relationship_kwargs={"remote_side": "Post.id"}
    )

    # IG comments + translations
    texts: List["PostText"] = Relationship(
        back_populates="post",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )

    # Link to Author object
    author_obj: Optional[Author] = Relationship(back_populates="posts")

    @property
    def author_name(self):
        return self.author_obj.name if self.author_obj else None

    @property
    def author_photo(self):
        return self.author_obj.profile_photo_url if self.author_obj else None

# ============================================================
# POST TEXT — IG replies, translations, general text entities
# ============================================================


class PostText(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    post_id: int = Field(foreign_key="post.id")

    type: str           # "ig-reply", "ig-translation"
    language: str       # "th", "en" etc.

    # REPLACES old author:str
    author_id: Optional[int] = Field(default=None, foreign_key="author.id")

    content: str
    posted_at: Optional[str] = None
    media_url: Optional[str] = None

    # IG threaded comments (optional)
    parent_comment_id: Optional[int] = Field(
        default=None, foreign_key="posttext.id"
    )

    # Relationships
    post: Optional[Post] = Relationship(back_populates="texts")
    author_obj: Optional[Author] = Relationship(back_populates="texts")
    @property
    def author_name(self):
        return self.author_obj.name if self.author_obj else None

    @property
    def author_photo(self):
        return self.author_obj.profile_photo_url if self.author_obj else None
