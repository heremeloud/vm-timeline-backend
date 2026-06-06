from datetime import datetime
import json
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from database import get_session
from middleware.auth import require_admin
from models import Topic, TopicItem, Post, Author

router = APIRouter(prefix="/topics", tags=["Topics"])


class TopicItemPayload(BaseModel):
    post_id: int
    happened_at: Optional[str] = None
    label: Optional[str] = None
    note: Optional[str] = None
    show_replies: bool = True
    media_index: Optional[int] = None
    sort_order: Optional[int] = None


class TopicCreate(BaseModel):
    title: str
    original_title: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    cover_url: Optional[str] = None
    is_public: bool = False
    is_visible: bool = True
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    sort_order: Optional[int] = None
    items: Optional[List[TopicItemPayload]] = None


class TopicUpdate(BaseModel):
    title: Optional[str] = None
    original_title: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    cover_url: Optional[str] = None
    is_public: Optional[bool] = None
    is_visible: Optional[bool] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    sort_order: Optional[int] = None
    items: Optional[List[TopicItemPayload]] = None


class TopicItemTimeUpdate(BaseModel):
    happened_at: Optional[str] = None


def _enrich_post(post: Post, author: Author | None) -> Dict[str, Any]:
    obj = post.dict()
    obj["author_name"] = author.name if author else None
    obj["author_photo"] = author.profile_photo_url if author else None

    try:
        raw = json.loads(post.media_urls_json or "[]")
        obj["media_urls"] = [
            {"url": item, "text": None, "translation": None, "note": None}
            if isinstance(item, str)
            else item
            for item in raw
        ]
    except Exception:
        obj["media_urls"] = []

    return obj


def _serialize_topic(session: Session, topic: Topic, include_items: bool = True) -> Dict[str, Any]:
    obj = topic.dict()

    items = []
    if include_items:
        rows = session.exec(
            select(TopicItem)
            .where(TopicItem.topic_id == topic.id)
            .order_by(TopicItem.sort_order, TopicItem.happened_at, TopicItem.id)
        ).all()

        for item in rows:
            post = session.get(Post, item.post_id)
            if not post:
                continue
            author = session.get(Author, post.author_id) if post.author_id else None
            item_obj = item.dict()
            item_obj["post"] = _enrich_post(post, author)
            items.append(item_obj)

    obj["items"] = items
    return obj


def _replace_items(session: Session, topic_id: int, items: List[TopicItemPayload]) -> None:
    old_items = session.exec(select(TopicItem).where(TopicItem.topic_id == topic_id)).all()
    for item in old_items:
        session.delete(item)
    session.commit()

    for index, payload in enumerate(items):
        post = session.get(Post, payload.post_id)
        if not post:
            raise HTTPException(status_code=400, detail=f"Unknown post_id: {payload.post_id}")

        session.add(
            TopicItem(
                topic_id=topic_id,
                post_id=payload.post_id,
                happened_at=(payload.happened_at.strip() if payload.happened_at else None),
                label=(payload.label.strip() if payload.label else None),
                note=(payload.note.strip() if payload.note else None),
                show_replies=payload.show_replies,
                media_index=payload.media_index,
                sort_order=payload.sort_order if payload.sort_order is not None else index,
            )
        )
    session.commit()


@router.get("/")
def list_topics(session: Session = Depends(get_session)):
    topics = session.exec(
        select(Topic)
        .where(Topic.is_visible == True)
        .order_by(Topic.sort_order.desc(), Topic.id.desc())
    ).all()
    return [_serialize_topic(session, topic, include_items=False) for topic in topics]


@router.get("/admin", dependencies=[Depends(require_admin)])
def list_admin_topics(session: Session = Depends(get_session)):
    topics = session.exec(select(Topic).order_by(Topic.sort_order.desc(), Topic.id.desc())).all()
    return [_serialize_topic(session, topic, include_items=False) for topic in topics]


@router.patch("/items/{item_id}/time", dependencies=[Depends(require_admin)])
def update_topic_item_time(
    item_id: int,
    payload: TopicItemTimeUpdate,
    session: Session = Depends(get_session),
):
    item = session.get(TopicItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Topic item not found")

    item.happened_at = payload.happened_at.strip() if payload.happened_at else None
    session.add(item)
    session.commit()
    session.refresh(item)
    return item.dict()


@router.get("/{topic_id}")
def get_topic(topic_id: int, session: Session = Depends(get_session)):
    topic = session.get(Topic, topic_id)
    if not topic or not topic.is_visible:
        raise HTTPException(status_code=404, detail="Topic not found")
    return {"topic": _serialize_topic(session, topic)}


@router.post("/", dependencies=[Depends(require_admin)])
def create_topic(payload: TopicCreate, session: Session = Depends(get_session)):
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")

    if payload.slug:
        existing = session.exec(select(Topic).where(Topic.slug == payload.slug.strip())).first()
        if existing:
            raise HTTPException(status_code=400, detail="Slug already exists")

    topic = Topic(
        title=title,
        original_title=(payload.original_title.strip() if payload.original_title else None),
        slug=(payload.slug.strip() if payload.slug else None),
        description=(payload.description.strip() if payload.description else None),
        cover_url=(payload.cover_url.strip() if payload.cover_url else None),
        is_public=False,
        is_visible=payload.is_visible,
        start_date=(payload.start_date.strip() if payload.start_date else None),
        end_date=(payload.end_date.strip() if payload.end_date else None),
        sort_order=payload.sort_order or 0,
        created_at=datetime.utcnow().isoformat(timespec="seconds"),
    )
    session.add(topic)
    session.commit()
    session.refresh(topic)

    if not payload.sort_order:
        topic.sort_order = topic.id or 0
        session.add(topic)
        session.commit()
        session.refresh(topic)

    _replace_items(session, topic.id, payload.items or [])
    return _serialize_topic(session, topic)


@router.patch("/{topic_id}", dependencies=[Depends(require_admin)])
def update_topic(topic_id: int, payload: TopicUpdate, session: Session = Depends(get_session)):
    topic = session.get(Topic, topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")

    if payload.title is not None:
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="Title cannot be empty")
        topic.title = title

    if payload.slug is not None:
        slug = payload.slug.strip() or None
        if slug:
            existing = session.exec(select(Topic).where(Topic.slug == slug, Topic.id != topic_id)).first()
            if existing:
                raise HTTPException(status_code=400, detail="Slug already exists")
        topic.slug = slug

    if payload.original_title is not None:
        topic.original_title = payload.original_title.strip() or None
    if payload.description is not None:
        topic.description = payload.description.strip() or None
    if payload.cover_url is not None:
        topic.cover_url = payload.cover_url.strip() or None
    if payload.is_public is not None:
        topic.is_public = False
    if payload.is_visible is not None:
        topic.is_visible = payload.is_visible
    if payload.start_date is not None:
        topic.start_date = payload.start_date.strip() or None
    if payload.end_date is not None:
        topic.end_date = payload.end_date.strip() or None
    if payload.sort_order is not None:
        topic.sort_order = payload.sort_order

    session.add(topic)
    session.commit()
    session.refresh(topic)

    if payload.items is not None:
        _replace_items(session, topic.id, payload.items)

    return _serialize_topic(session, topic)


@router.delete("/{topic_id}", dependencies=[Depends(require_admin)])
def delete_topic(topic_id: int, session: Session = Depends(get_session)):
    topic = session.get(Topic, topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")

    items = session.exec(select(TopicItem).where(TopicItem.topic_id == topic_id)).all()
    for item in items:
        session.delete(item)

    session.delete(topic)
    session.commit()
    return {"status": "deleted", "id": topic_id}
