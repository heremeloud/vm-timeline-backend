from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select, desc
from typing import Optional, List, Any, Dict
import json

from database import get_session
from models import Event, Author, EventAuthorLink, Project
from middleware.auth import require_admin
from constants import EVENT_CATEGORIES

router = APIRouter(prefix="/events", tags=["Events"])


# ----------------------------
# Helpers
# ----------------------------
def _safe_parse_tags(tags_json: str) -> List[str]:
    try:
        data = json.loads(tags_json or "[]")
        if isinstance(data, list):
            return [str(x) for x in data if isinstance(x, (str, int, float))]
        return []
    except Exception:
        return []


def _safe_dump_tags(tags: Optional[List[str]]) -> str:
    if not tags:
        return "[]"
    clean = []
    for t in tags:
        if t is None:
            continue
        s = str(t).strip()
        if s:
            clean.append(s)
    return json.dumps(clean, ensure_ascii=False)


def _serialize_event(session: Session, ev: Event) -> Dict[str, Any]:
    # Load participants via link table
    links = session.exec(
        select(EventAuthorLink).where(EventAuthorLink.event_id == ev.id)
    ).all()
    author_ids = [l.author_id for l in links if l.author_id is not None]

    authors = []
    if author_ids:
        authors = session.exec(
            select(Author).where(Author.id.in_(author_ids))
        ).all()

        # keep same order as author_ids
        by_id = {a.id: a for a in authors}
        authors = [by_id[i] for i in author_ids if i in by_id]

    obj = ev.dict()

    # ensure tags come back as list
    obj["tags"] = _safe_parse_tags(getattr(ev, "tags_json", "[]"))

    # ensure live_urls comes back as list
    raw = getattr(ev, "live_urls", "") or ""
    obj["live_urls"] = [u.strip() for u in raw.split(",") if u.strip()]

    # ensure these always present in response
    obj["announcement_url"] = getattr(ev, "announcement_url", None)
    obj["project_id"] = getattr(ev, "project_id", None)

    obj["authors"] = [
        {
            "id": a.id,
            "name": a.name,
            "profile_photo_url": a.profile_photo_url or a.ig_pfp_url or a.twitter_pfp_url,
            "ig_pfp_url": a.ig_pfp_url,
            "twitter_pfp_url": a.twitter_pfp_url,
            "tiktok_pfp_url": a.tiktok_pfp_url,
        }
        for a in authors
    ]

    # Parent press tour info
    if ev.parent_event_id:
        parent_ev = session.get(Event, ev.parent_event_id)
        obj["parent_event_id"] = ev.parent_event_id
        obj["parent_event_name"] = parent_ev.name if parent_ev else None
    else:
        obj["parent_event_id"] = None
        obj["parent_event_name"] = None

    # Child events (interviews inside a press tour)
    children = session.exec(
        select(Event).where(Event.parent_event_id == ev.id).order_by(Event.event_date, Event.id)
    ).all()
    obj["child_events"] = [
        {"id": c.id, "name": c.name, "event_date": c.event_date, "category": c.category}
        for c in children
    ]

    # Attach linked project info if present
    if ev.project_id:
        proj = session.get(Project, ev.project_id)
        obj["project_title"] = proj.title if proj else None
        obj["project_thumbnail_url"] = proj.thumbnail_url if proj else None
        obj["project_category"] = proj.category if proj else None
    else:
        obj["project_title"] = None
        obj["project_thumbnail_url"] = None
        obj["project_category"] = None

    return obj


def _ensure_authors_exist(session: Session, author_ids: List[int]) -> List[Author]:
    if not author_ids:
        return []

    # remove duplicates while preserving order
    uniq = list(dict.fromkeys(author_ids))

    authors = session.exec(select(Author).where(Author.id.in_(uniq))).all()
    found = {a.id for a in authors}

    missing = [aid for aid in uniq if aid not in found]
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown author_id(s): {missing}")

    by_id = {a.id: a for a in authors}
    return [by_id[i] for i in uniq]


# ----------------------------
# Schemas (request bodies)
# ----------------------------
from pydantic import BaseModel


VALID_CATEGORIES = set(EVENT_CATEGORIES)


def _field_was_sent(payload: BaseModel, field_name: str) -> bool:
    fields_set = getattr(payload, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(payload, "__fields_set__", set())
    return field_name in fields_set


class EventCreate(BaseModel):
    name: str
    location: Optional[str] = None
    keyword: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    media_url: Optional[str] = None
    event_date: Optional[str] = None  # YYYY-MM-DD
    announcement_url: Optional[str] = None
    live_urls: Optional[List[str]] = None
    author_ids: Optional[List[int]] = None
    project_id: Optional[int] = None
    parent_event_id: Optional[int] = None
    is_visible: bool = True


class EventUpdate(BaseModel):
    name: Optional[str] = None
    location: Optional[str] = None
    keyword: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    media_url: Optional[str] = None
    event_date: Optional[str] = None
    announcement_url: Optional[str] = None
    live_urls: Optional[List[str]] = None
    author_ids: Optional[List[int]] = None
    project_id: Optional[int] = None
    parent_event_id: Optional[int] = None
    is_visible: Optional[bool] = None


# ----------------------------
# GET CATEGORIES
# ----------------------------
@router.get("/categories")
def list_categories():
    return {"categories": EVENT_CATEGORIES}


@router.get("/admin", dependencies=[Depends(require_admin)])
def list_admin_events(
    sort: str = "newest",
    offset: int = 0,
    limit: int = 50,
    name: Optional[str] = None,
    category: Optional[str] = None,
    session: Session = Depends(get_session),
):
    query = select(Event)

    if name:
        query = query.where(Event.name.ilike(f"%{name.strip()}%"))

    if category:
        query = query.where(Event.category == category.strip().lower())

    if sort == "oldest":
        query = query.order_by(Event.event_date, Event.id)
    else:
        query = query.order_by(desc(Event.event_date), desc(Event.id))

    events = session.exec(query.offset(offset).limit(limit)).all()
    return [_serialize_event(session, ev) for ev in events]


# ----------------------------
# GET LIST (pagination + filters)
# ----------------------------
@router.get("/")
def list_events(
    sort: str = "newest",
    offset: int = 0,
    limit: int = 10,
    name: Optional[str] = None,
    keyword: Optional[str] = None,
    tag: Optional[str] = None,
    category: Optional[str] = None,
    author: Optional[str] = None,   # "view", "mim", or "viewmim"
    session: Session = Depends(get_session),
):
    query = select(Event).where(Event.is_visible == True)

    if name:
        query = query.where(Event.name.ilike(f"%{name.strip()}%"))

    if keyword:
        query = query.where(Event.keyword == keyword)

    if tag:
        needle = f'"{tag}"'
        query = query.where(Event.tags_json.contains(needle))

    if category:
        query = query.where(Event.category == category.strip().lower())

    if author:
        author_lower = author.strip().lower()
        view = session.exec(select(Author).where(Author.name.ilike("view"))).first()
        mim  = session.exec(select(Author).where(Author.name.ilike("mim"))).first()
        view_event_ids = {l.event_id for l in session.exec(select(EventAuthorLink).where(EventAuthorLink.author_id == view.id)).all()} if view else set()
        mim_event_ids  = {l.event_id for l in session.exec(select(EventAuthorLink).where(EventAuthorLink.author_id == mim.id)).all()} if mim else set()

        if author_lower == "viewmim":
            # Events with BOTH View and Mim (couple events)
            allowed = list(view_event_ids & mim_event_ids)
        elif author_lower == "view":
            # Events with View but NOT Mim (solo View)
            allowed = list(view_event_ids - mim_event_ids)
        elif author_lower == "mim":
            # Events with Mim but NOT View (solo Mim)
            allowed = list(mim_event_ids - view_event_ids)
        else:
            allowed = []

        query = query.where(Event.id.in_(allowed)) if allowed else query.where(Event.id == -1)

    if sort == "oldest":
        query = query.order_by(Event.event_date, Event.id)
    else:
        query = query.order_by(desc(Event.event_date), desc(Event.id))

    query = query.offset(offset).limit(limit)

    events = session.exec(query).all()
    return [_serialize_event(session, ev) for ev in events]


# ----------------------------
# GET ONE
# ----------------------------
@router.get("/{event_id}")
def get_event(event_id: int, session: Session = Depends(get_session)):
    ev = session.get(Event, event_id)
    if not ev:
        raise HTTPException(status_code=404, detail="Event not found")
    return {"event": _serialize_event(session, ev)}


# ----------------------------
# CREATE
# ----------------------------
@router.post("/", dependencies=[Depends(require_admin)])
def create_event(payload: EventCreate, session: Session = Depends(get_session)):
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Event name is required")

    authors = _ensure_authors_exist(session, payload.author_ids or [])

    category = (payload.category.strip().lower() if payload.category else None)
    if category and category not in VALID_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Invalid category. Must be one of: {', '.join(sorted(VALID_CATEGORIES))}")

    ev = Event(
        name=name,
        location=(payload.location.strip() if payload.location else None),
        keyword=(payload.keyword.strip() if payload.keyword else None),
        category=category,
        tags_json=_safe_dump_tags(payload.tags),
        media_url=(payload.media_url.strip() if payload.media_url else None),
        event_date=(payload.event_date.strip() if payload.event_date else None),
        announcement_url=(payload.announcement_url.strip() if payload.announcement_url else None),
        live_urls=",".join(u.strip() for u in (payload.live_urls or []) if u.strip()),
        project_id=payload.project_id,
        parent_event_id=payload.parent_event_id,
        is_visible=payload.is_visible,
    )

    session.add(ev)
    session.commit()
    session.refresh(ev)

    # Insert links
    for a in authors:
        session.add(EventAuthorLink(event_id=ev.id, author_id=a.id))
    session.commit()
    session.refresh(ev)

    return _serialize_event(session, ev)


# ----------------------------
# UPDATE
# ----------------------------
@router.patch("/{event_id}", dependencies=[Depends(require_admin)])
def update_event(event_id: int, payload: EventUpdate, session: Session = Depends(get_session)):
    ev = session.get(Event, event_id)
    if not ev:
        raise HTTPException(status_code=404, detail="Event not found")

    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Event name cannot be empty")
        ev.name = name

    if _field_was_sent(payload, "location"):
        ev.location = payload.location.strip() if payload.location else None

    if _field_was_sent(payload, "keyword"):
        ev.keyword = payload.keyword.strip() if payload.keyword else None

    if _field_was_sent(payload, "category"):
        cat = payload.category.strip().lower() if payload.category else None
        if cat and cat not in VALID_CATEGORIES:
            raise HTTPException(status_code=400, detail=f"Invalid category. Must be one of: {', '.join(sorted(VALID_CATEGORIES))}")
        ev.category = cat

    if _field_was_sent(payload, "media_url"):
        ev.media_url = payload.media_url.strip() if payload.media_url else None

    if _field_was_sent(payload, "event_date"):
        ev.event_date = payload.event_date.strip() if payload.event_date else None

    if payload.tags is not None:
        ev.tags_json = _safe_dump_tags(payload.tags)

    if _field_was_sent(payload, "announcement_url"):
        ev.announcement_url = payload.announcement_url.strip() if payload.announcement_url else None

    if payload.live_urls is not None:
        ev.live_urls = ",".join(u.strip() for u in payload.live_urls if u.strip())

    if payload.project_id is not None:
        ev.project_id = payload.project_id
    elif _field_was_sent(payload, "project_id"):
        ev.project_id = None  # explicitly cleared

    if payload.parent_event_id is not None:
        ev.parent_event_id = payload.parent_event_id
    elif _field_was_sent(payload, "parent_event_id"):
        ev.parent_event_id = None  # explicitly cleared

    if payload.is_visible is not None:
        ev.is_visible = payload.is_visible

    session.add(ev)
    session.commit()
    session.refresh(ev)

    # Replace participants if author_ids provided
    if payload.author_ids is not None:
        authors = _ensure_authors_exist(session, payload.author_ids)

        old_links = session.exec(
            select(EventAuthorLink).where(EventAuthorLink.event_id == event_id)
        ).all()
        for l in old_links:
            session.delete(l)
        session.commit()

        for a in authors:
            session.add(EventAuthorLink(event_id=event_id, author_id=a.id))
        session.commit()

    return _serialize_event(session, ev)


# ----------------------------
# DELETE
# ----------------------------
@router.delete("/{event_id}", dependencies=[Depends(require_admin)])
def delete_event(event_id: int, session: Session = Depends(get_session)):
    ev = session.get(Event, event_id)
    if not ev:
        raise HTTPException(status_code=404, detail="Event not found")

    links = session.exec(
        select(EventAuthorLink).where(EventAuthorLink.event_id == event_id)
    ).all()
    for l in links:
        session.delete(l)

    session.delete(ev)
    session.commit()
    return {"status": "deleted", "id": event_id}
