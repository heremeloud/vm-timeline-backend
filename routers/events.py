from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select, desc
from typing import Optional, List, Any, Dict
import json

from database import get_session
from models import Event, Author, EventAuthorLink
from middleware.auth import require_admin

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

    # ensure these always present in response
    obj["announcement_url"] = getattr(ev, "announcement_url", None)
    obj["live_url"] = getattr(ev, "live_url", None)  

    obj["authors"] = [
        {"id": a.id, "name": a.name, "profile_photo_url": a.profile_photo_url}
        for a in authors
    ]
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


class EventCreate(BaseModel):
    name: str
    location: Optional[str] = None
    keyword: Optional[str] = None
    tags: Optional[List[str]] = None
    media_url: Optional[str] = None
    event_date: Optional[str] = None  # YYYY-MM-DD
    announcement_url: Optional[str] = None
    live_url: Optional[str] = None          
    author_ids: Optional[List[int]] = None


class EventUpdate(BaseModel):
    name: Optional[str] = None
    location: Optional[str] = None
    keyword: Optional[str] = None
    tags: Optional[List[str]] = None
    media_url: Optional[str] = None
    event_date: Optional[str] = None
    announcement_url: Optional[str] = None
    live_url: Optional[str] = None          
    author_ids: Optional[List[int]] = None


# ----------------------------
# GET LIST (pagination + filters)
# ----------------------------
@router.get("/")
def list_events(
    sort: str = "newest",
    offset: int = 0,
    limit: int = 10,
    keyword: Optional[str] = None,
    tag: Optional[str] = None,
    session: Session = Depends(get_session),
):
    query = select(Event)

    if keyword:
        query = query.where(Event.keyword == keyword)

    if tag:
        needle = f'"{tag}"'
        query = query.where(Event.tags_json.contains(needle))

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

    ev = Event(
        name=name,
        location=(payload.location.strip() if payload.location else None),
        keyword=(payload.keyword.strip() if payload.keyword else None),
        tags_json=_safe_dump_tags(payload.tags),
        media_url=(payload.media_url.strip() if payload.media_url else None),
        event_date=(payload.event_date.strip() if payload.event_date else None),
        announcement_url=(payload.announcement_url.strip() if payload.announcement_url else None),
        live_url=(payload.live_url.strip() if payload.live_url else None),  
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

    if payload.location is not None:
        ev.location = payload.location.strip() or None

    if payload.keyword is not None:
        ev.keyword = payload.keyword.strip() or None

    if payload.media_url is not None:
        ev.media_url = payload.media_url.strip() or None

    if payload.event_date is not None:
        ev.event_date = payload.event_date.strip() or None

    if payload.tags is not None:
        ev.tags_json = _safe_dump_tags(payload.tags)

    if payload.announcement_url is not None:
        ev.announcement_url = payload.announcement_url.strip() or None

    if payload.live_url is not None:  # ✅ NEW
        ev.live_url = payload.live_url.strip() or None

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
