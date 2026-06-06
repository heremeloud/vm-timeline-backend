import json
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select, desc
from sqlalchemy import case, nullslast, nullsfirst
from typing import Optional, List, Any, Dict
from pydantic import BaseModel

from database import get_session
from models import Project, Author, ProjectAuthorLink, Event
from middleware.auth import require_admin
from constants import PROJECT_CATEGORIES

router = APIRouter(prefix="/projects", tags=["Projects"])

VALID_CATEGORIES = set(PROJECT_CATEGORIES)


# ----------------------------
# Helpers
# ----------------------------

def _serialize_project(session: Session, p: Project) -> Dict[str, Any]:
    links = session.exec(
        select(ProjectAuthorLink).where(ProjectAuthorLink.project_id == p.id)
    ).all()
    author_ids = [l.author_id for l in links if l.author_id is not None]

    authors = []
    if author_ids:
        rows = session.exec(select(Author).where(Author.id.in_(author_ids))).all()
        by_id = {a.id: a for a in rows}
        authors = [by_id[i] for i in author_ids if i in by_id]

    # Build playlists array: normalize to {name?, id} objects
    try:
        raw = json.loads(p.playlists_json or "[]")
    except Exception:
        raw = []

    playlists = []
    seen_ids = set()
    for entry in raw:
        if isinstance(entry, str) and entry.strip():
            pid = entry.strip()
            if pid not in seen_ids:
                playlists.append({"id": pid})
                seen_ids.add(pid)
        elif isinstance(entry, dict) and entry.get("id"):
            pid = entry["id"]
            if pid not in seen_ids:
                playlists.append(entry)
                seen_ids.add(pid)

    # Legacy playlist_id field
    if p.playlist_id and p.playlist_id not in seen_ids:
        playlists = [{"id": p.playlist_id}] + playlists

    # Linked events
    linked_events = session.exec(
        select(Event).where(Event.project_id == p.id).order_by(Event.event_date)
    ).all()

    obj = p.dict()
    obj["playlists"] = playlists
    obj["authors"] = [
        {"id": a.id, "name": a.name, "profile_photo_url": a.profile_photo_url}
        for a in authors
    ]
    obj["youtube_url"] = p.youtube_url
    obj["spotify_url"] = p.spotify_url
    obj["apple_music_url"] = p.apple_music_url

    # Parent project
    parent_project = None
    if p.parent_project_id:
        pp = session.get(Project, p.parent_project_id)
        if pp:
            parent_project = {"id": pp.id, "title": pp.title, "thumbnail_url": pp.thumbnail_url, "category": pp.category}
    obj["parent_project_id"] = p.parent_project_id
    obj["parent_project"] = parent_project

    # Child projects
    children = session.exec(select(Project).where(Project.parent_project_id == p.id)).all()
    obj["child_projects"] = [
        {"id": c.id, "title": c.title, "thumbnail_url": c.thumbnail_url, "category": c.category}
        for c in children
    ]

    obj["events"] = [
        {"id": e.id, "name": e.name, "event_date": e.event_date, "category": e.category, "parent_event_id": e.parent_event_id}
        for e in linked_events
    ]
    return obj


def _ensure_authors(session: Session, author_ids: List[int]) -> List[Author]:
    if not author_ids:
        return []
    uniq = list(dict.fromkeys(author_ids))
    rows = session.exec(select(Author).where(Author.id.in_(uniq))).all()
    found = {a.id for a in rows}
    missing = [i for i in uniq if i not in found]
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown author_id(s): {missing}")
    by_id = {a.id: a for a in rows}
    return [by_id[i] for i in uniq]


# ----------------------------
# Schemas
# ----------------------------

class ProjectCreate(BaseModel):
    title: str
    original_title: Optional[str] = None
    category: Optional[str] = None
    thumbnail_url: Optional[str] = None
    is_visible: bool = True
    year: Optional[int] = None
    description: Optional[str] = None
    playlist_ids: Optional[List[Any]] = None   # list of {name, id} objects or plain ID strings
    announcement_url: Optional[str] = None
    tweet_url: Optional[str] = None
    youtube_url: Optional[str] = None
    mydramalist_url: Optional[str] = None
    gmmtv_url: Optional[str] = None
    spotify_url: Optional[str] = None
    apple_music_url: Optional[str] = None
    parent_project_id: Optional[int] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    author_ids: Optional[List[int]] = None


class ProjectUpdate(BaseModel):
    title: Optional[str] = None
    original_title: Optional[str] = None
    category: Optional[str] = None
    thumbnail_url: Optional[str] = None
    is_visible: Optional[bool] = None
    year: Optional[int] = None
    description: Optional[str] = None
    playlist_ids: Optional[List[Any]] = None   # list of {name, id} objects or plain ID strings
    announcement_url: Optional[str] = None
    tweet_url: Optional[str] = None
    youtube_url: Optional[str] = None
    mydramalist_url: Optional[str] = None
    gmmtv_url: Optional[str] = None
    spotify_url: Optional[str] = None
    apple_music_url: Optional[str] = None
    parent_project_id: Optional[int] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    author_ids: Optional[List[int]] = None


# ----------------------------
# GET categories
# ----------------------------

@router.get("/categories")
def list_categories():
    return {"categories": PROJECT_CATEGORIES}


@router.get("/admin", dependencies=[Depends(require_admin)])
def list_admin_projects(
    sort: str = "newest",
    category: Optional[str] = None,
    offset: int = 0,
    limit: int = 50,
    session: Session = Depends(get_session),
):
    query = select(Project)

    if category:
        query = query.where(Project.category == category.strip().lower())

    if sort == "oldest":
        query = query.order_by(nullslast(Project.start_date.asc()), Project.id.asc())
    else:
        query = query.order_by(nullsfirst(Project.start_date.desc()), Project.id.desc())

    projects = session.exec(query.offset(offset).limit(limit)).all()
    return [_serialize_project(session, p) for p in projects]


# ----------------------------
# GET list
# ----------------------------

@router.get("/")
def list_projects(
    sort: str = "newest",
    category: Optional[str] = None,
    session: Session = Depends(get_session),
):
    query = select(Project).where(Project.is_visible == True)

    if category:
        query = query.where(Project.category == category.strip().lower())

    if sort == "oldest":
        query = query.order_by(nullslast(Project.start_date.asc()), Project.id.asc())
    else:
        query = query.order_by(nullsfirst(Project.start_date.desc()), Project.id.desc())

    projects = session.exec(query).all()
    return [_serialize_project(session, p) for p in projects]


# ----------------------------
# GET one
# ----------------------------

@router.get("/{project_id}")
def get_project(project_id: int, session: Session = Depends(get_session)):
    p = session.get(Project, project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"project": _serialize_project(session, p)}


# ----------------------------
# CREATE
# ----------------------------

@router.post("/", dependencies=[Depends(require_admin)])
def create_project(payload: ProjectCreate, session: Session = Depends(get_session)):
    title = (payload.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")

    category = payload.category.strip().lower() if payload.category else None
    if category and category not in VALID_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Invalid category. Must be one of: {', '.join(sorted(VALID_CATEGORIES))}")

    authors = _ensure_authors(session, payload.author_ids or [])

    # Normalize playlists: accept both plain strings and {name, id} dicts
    raw_playlists = payload.playlist_ids or []
    playlist_objs = []
    for entry in raw_playlists:
        if isinstance(entry, str) and entry.strip():
            playlist_objs.append({"id": entry.strip()})
        elif isinstance(entry, dict) and entry.get("id"):
            playlist_objs.append(entry)

    p = Project(
        title=title,
        original_title=(payload.original_title.strip() if payload.original_title else None),
        category=category,
        thumbnail_url=(payload.thumbnail_url.strip() if payload.thumbnail_url else None),
        is_visible=payload.is_visible,
        year=payload.year,
        description=(payload.description.strip() if payload.description else None),
        playlists_json=json.dumps(playlist_objs),
        announcement_url=(payload.announcement_url.strip() if payload.announcement_url else None),
        tweet_url=(payload.tweet_url.strip() if payload.tweet_url else None),
        youtube_url=(payload.youtube_url.strip() if payload.youtube_url else None),
        mydramalist_url=(payload.mydramalist_url.strip() if payload.mydramalist_url else None),
        gmmtv_url=(payload.gmmtv_url.strip() if payload.gmmtv_url else None),
        spotify_url=(payload.spotify_url.strip() if payload.spotify_url else None),
        apple_music_url=(payload.apple_music_url.strip() if payload.apple_music_url else None),
        parent_project_id=payload.parent_project_id or None,
        start_date=(payload.start_date.strip() if payload.start_date else None),
        end_date=(payload.end_date.strip() if payload.end_date else None),
    )
    session.add(p)
    session.commit()
    session.refresh(p)

    for a in authors:
        session.add(ProjectAuthorLink(project_id=p.id, author_id=a.id))
    session.commit()

    return _serialize_project(session, p)


# ----------------------------
# UPDATE
# ----------------------------

@router.patch("/{project_id}", dependencies=[Depends(require_admin)])
def update_project(project_id: int, payload: ProjectUpdate, session: Session = Depends(get_session)):
    p = session.get(Project, project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")

    if payload.title is not None:
        t = payload.title.strip()
        if not t:
            raise HTTPException(status_code=400, detail="Title cannot be empty")
        p.title = t

    if payload.category is not None:
        cat = payload.category.strip().lower() or None
        if cat and cat not in VALID_CATEGORIES:
            raise HTTPException(status_code=400, detail=f"Invalid category. Must be one of: {', '.join(sorted(VALID_CATEGORIES))}")
        p.category = cat

    if payload.original_title is not None:
        p.original_title = payload.original_title.strip() or None
    if payload.thumbnail_url is not None:
        p.thumbnail_url = payload.thumbnail_url.strip() or None
    if payload.is_visible is not None:
        p.is_visible = payload.is_visible
    if payload.year is not None:
        p.year = payload.year
    if payload.description is not None:
        p.description = payload.description.strip() or None
    if payload.playlist_ids is not None:
        playlist_objs = []
        for entry in payload.playlist_ids:
            if isinstance(entry, str) and entry.strip():
                playlist_objs.append({"id": entry.strip()})
            elif isinstance(entry, dict) and entry.get("id"):
                playlist_objs.append(entry)
        p.playlists_json = json.dumps(playlist_objs)
    if payload.announcement_url is not None:
        p.announcement_url = payload.announcement_url.strip() or None
    if payload.tweet_url is not None:
        p.tweet_url = payload.tweet_url.strip() or None
    if payload.youtube_url is not None:
        p.youtube_url = payload.youtube_url.strip() or None
    if payload.mydramalist_url is not None:
        p.mydramalist_url = payload.mydramalist_url.strip() or None
    if payload.gmmtv_url is not None:
        p.gmmtv_url = payload.gmmtv_url.strip() or None
    if payload.spotify_url is not None:
        p.spotify_url = payload.spotify_url.strip() or None
    if payload.apple_music_url is not None:
        p.apple_music_url = payload.apple_music_url.strip() or None
    if payload.parent_project_id is not None:
        p.parent_project_id = payload.parent_project_id or None
    if payload.start_date is not None:
        p.start_date = payload.start_date.strip() or None
    if payload.end_date is not None:
        p.end_date = payload.end_date.strip() or None

    session.add(p)
    session.commit()
    session.refresh(p)

    if payload.author_ids is not None:
        old = session.exec(select(ProjectAuthorLink).where(ProjectAuthorLink.project_id == project_id)).all()
        for l in old:
            session.delete(l)
        session.commit()
        for a in _ensure_authors(session, payload.author_ids):
            session.add(ProjectAuthorLink(project_id=project_id, author_id=a.id))
        session.commit()

    return _serialize_project(session, p)


# ----------------------------
# DELETE
# ----------------------------

@router.delete("/{project_id}", dependencies=[Depends(require_admin)])
def delete_project(project_id: int, session: Session = Depends(get_session)):
    p = session.get(Project, project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")

    links = session.exec(select(ProjectAuthorLink).where(ProjectAuthorLink.project_id == project_id)).all()
    for l in links:
        session.delete(l)

    session.delete(p)
    session.commit()
    return {"status": "deleted", "id": project_id}
