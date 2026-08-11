import json
import re
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select, desc
from sqlalchemy import case, func, nullslast, nullsfirst
from typing import Optional, List, Any, Dict
from pydantic import BaseModel

from database import get_session
from models import Project, ProjectFilmingDay, ProjectEpisode, Author, ProjectAuthorLink, Event
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
        select(Event).where(Event.project_id == p.id).order_by(Event.start_date)
    ).all()

    obj = p.dict()
    obj["playlists"] = playlists
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
    obj["youtube_url"] = p.youtube_url
    obj["spotify_url"] = p.spotify_url
    obj["apple_music_url"] = p.apple_music_url

    # Parent project
    parent_project = None
    if p.parent_project_id:
        pp = session.get(Project, p.parent_project_id)
        if pp:
            parent_project = {
                "id": pp.id,
                "title": pp.title,
                "slug": pp.slug,
                "thumbnail_url": pp.thumbnail_url,
                "thumbnail_focal_x": pp.thumbnail_focal_x,
                "thumbnail_focal_y": pp.thumbnail_focal_y,
                "category": pp.category,
            }
    obj["parent_project_id"] = p.parent_project_id
    obj["parent_project"] = parent_project

    # Child projects
    children = session.exec(select(Project).where(Project.parent_project_id == p.id)).all()
    obj["child_projects"] = [
        {
            "id": c.id,
            "title": c.title,
            "slug": c.slug,
            "thumbnail_url": c.thumbnail_url,
            "thumbnail_focal_x": c.thumbnail_focal_x,
            "thumbnail_focal_y": c.thumbnail_focal_y,
            "category": c.category,
        }
        for c in children
    ]

    obj["events"] = [
        {
            "id": e.id,
            "name": e.name,
            "english_name": e.english_name,
            "event_date": e.event_date,
            "start_date": e.start_date or e.event_date,
            "end_date": e.end_date,
            "category": e.category,
            "subcategory": e.subcategory,
            "parent_event_id": e.parent_event_id,
        }
        for e in linked_events
    ]
    filming_days = session.exec(
        select(ProjectFilmingDay)
        .where(ProjectFilmingDay.project_id == p.id)
        .order_by(ProjectFilmingDay.q_number, ProjectFilmingDay.id)
    ).all()
    episodes = session.exec(
        select(ProjectEpisode)
        .where(ProjectEpisode.project_id == p.id)
        .order_by(ProjectEpisode.episode_number, ProjectEpisode.id)
    ).all()
    obj["filming_days"] = [row.dict() for row in filming_days]
    obj["episode_metadata"] = [row.dict() for row in episodes]
    return obj


def _serialize_project_list(session: Session, projects: List[Project]) -> List[Dict[str, Any]]:
    """Return project-card data without loading detail-only collections."""
    project_ids = [project.id for project in projects if project.id is not None]
    if not project_ids:
        return []

    links = session.exec(
        select(ProjectAuthorLink).where(ProjectAuthorLink.project_id.in_(project_ids))
    ).all()
    author_ids = {link.author_id for link in links if link.author_id is not None}
    authors = session.exec(select(Author).where(Author.id.in_(author_ids))).all() if author_ids else []
    authors_by_id = {author.id: author for author in authors}
    authors_by_project = {project_id: [] for project_id in project_ids}
    for link in links:
        author = authors_by_id.get(link.author_id)
        if author:
            authors_by_project.setdefault(link.project_id, []).append(author)

    parent_ids = {project.parent_project_id for project in projects if project.parent_project_id is not None}
    parents = session.exec(select(Project).where(Project.id.in_(parent_ids))).all() if parent_ids else []
    parents_by_id = {parent.id: parent for parent in parents}

    serialized = []
    for project in projects:
        obj = {
            "id": project.id,
            "title": project.title,
            "original_title": project.original_title,
            "hashtag": project.hashtag,
            "slug": project.slug,
            "category": project.category,
            "thumbnail_url": project.thumbnail_url,
            "thumbnail_focal_x": project.thumbnail_focal_x,
            "thumbnail_focal_y": project.thumbnail_focal_y,
            "is_visible": project.is_visible,
            "year": project.year,
            "start_date": project.start_date,
            "end_date": project.end_date,
            "parent_project_id": project.parent_project_id,
        }
        obj["authors"] = [
            {
                "id": author.id,
                "name": author.name,
                "profile_photo_url": author.profile_photo_url or author.ig_pfp_url or author.twitter_pfp_url,
                "ig_pfp_url": author.ig_pfp_url,
                "twitter_pfp_url": author.twitter_pfp_url,
                "tiktok_pfp_url": author.tiktok_pfp_url,
            }
            for author in authors_by_project.get(project.id, [])
        ]
        parent = parents_by_id.get(project.parent_project_id)
        obj["parent_project"] = {
            "id": parent.id,
            "title": parent.title,
            "slug": parent.slug,
            "thumbnail_url": parent.thumbnail_url,
            "thumbnail_focal_x": parent.thumbnail_focal_x,
            "thumbnail_focal_y": parent.thumbnail_focal_y,
            "category": parent.category,
        } if parent else None
        serialized.append(obj)
    return serialized


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


def _normalize_slug(slug: Optional[str]) -> Optional[str]:
    if not slug:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "-", slug.strip().lower())
    normalized = normalized.strip("-")
    return normalized or None


def _get_project_by_ref(session: Session, project_ref: str) -> Optional[Project]:
    if project_ref.isdigit():
        return session.get(Project, int(project_ref))
    return session.exec(select(Project).where(Project.slug == project_ref.strip().lower())).first()


def _clean_hashtag(value: Optional[str]) -> Optional[str]:
    clean = (value or "").strip().lstrip("#").replace(" ", "")
    return clean or None


def _replace_series_metadata(session: Session, project_id: int, filming_days, episodes) -> None:
    old_qs = session.exec(
        select(ProjectFilmingDay).where(ProjectFilmingDay.project_id == project_id)
    ).all()
    old_episodes = session.exec(
        select(ProjectEpisode).where(ProjectEpisode.project_id == project_id)
    ).all()
    for row in [*old_qs, *old_episodes]:
        session.delete(row)

    seen_qs = set()
    for row in filming_days or []:
        if row.q_number < 1 or row.q_number in seen_qs:
            raise HTTPException(status_code=400, detail="Q numbers must be positive and unique")
        seen_qs.add(row.q_number)
        session.add(ProjectFilmingDay(
            project_id=project_id,
            q_number=row.q_number,
            filming_date=(row.filming_date or "").strip() or None,
            hashtag=_clean_hashtag(row.hashtag),
            keyword=(row.keyword or "").strip() or None,
        ))

    seen_episodes = set()
    for row in episodes or []:
        if row.episode_number < 0 or row.episode_number in seen_episodes:
            raise HTTPException(status_code=400, detail="Episode numbers must be zero or greater and unique")
        seen_episodes.add(row.episode_number)
        session.add(ProjectEpisode(
            project_id=project_id,
            episode_number=row.episode_number,
            air_date=(row.air_date or "").strip() or None,
            title=(row.title or "").strip() or None,
            hashtag=_clean_hashtag(row.hashtag),
            keyword=(row.keyword or "").strip() or None,
        ))


# ----------------------------
# Schemas
# ----------------------------

class ProjectFilmingDayInput(BaseModel):
    q_number: int
    filming_date: Optional[str] = None
    hashtag: Optional[str] = None
    keyword: Optional[str] = None


class ProjectEpisodeInput(BaseModel):
    episode_number: int
    air_date: Optional[str] = None
    title: Optional[str] = None
    hashtag: Optional[str] = None
    keyword: Optional[str] = None


class ProjectCreate(BaseModel):
    title: str
    original_title: Optional[str] = None
    hashtag: Optional[str] = None
    slug: Optional[str] = None
    category: Optional[str] = None
    thumbnail_url: Optional[str] = None
    thumbnail_focal_x: Optional[float] = None
    thumbnail_focal_y: Optional[float] = None
    is_visible: bool = True
    year: Optional[int] = None
    episode_count: Optional[int] = None
    description: Optional[str] = None
    playlist_ids: Optional[List[Any]] = None   # list of {name, id} objects or plain ID strings
    announcement_url: Optional[str] = None
    tweet_url: Optional[str] = None
    tweet_label: Optional[str] = None
    youtube_url: Optional[str] = None
    youtube_label: Optional[str] = None
    mydramalist_url: Optional[str] = None
    gmmtv_url: Optional[str] = None
    official_twitter_url: Optional[str] = None
    spotify_url: Optional[str] = None
    apple_music_url: Optional[str] = None
    parent_project_id: Optional[int] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    author_ids: Optional[List[int]] = None
    filming_days: Optional[List[ProjectFilmingDayInput]] = None
    episode_metadata: Optional[List[ProjectEpisodeInput]] = None


class ProjectUpdate(BaseModel):
    title: Optional[str] = None
    original_title: Optional[str] = None
    hashtag: Optional[str] = None
    slug: Optional[str] = None
    category: Optional[str] = None
    thumbnail_url: Optional[str] = None
    thumbnail_focal_x: Optional[float] = None
    thumbnail_focal_y: Optional[float] = None
    is_visible: Optional[bool] = None
    year: Optional[int] = None
    episode_count: Optional[int] = None
    description: Optional[str] = None
    playlist_ids: Optional[List[Any]] = None   # list of {name, id} objects or plain ID strings
    announcement_url: Optional[str] = None
    tweet_url: Optional[str] = None
    tweet_label: Optional[str] = None
    youtube_url: Optional[str] = None
    youtube_label: Optional[str] = None
    mydramalist_url: Optional[str] = None
    gmmtv_url: Optional[str] = None
    official_twitter_url: Optional[str] = None
    spotify_url: Optional[str] = None
    apple_music_url: Optional[str] = None
    parent_project_id: Optional[int] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    author_ids: Optional[List[int]] = None
    filming_days: Optional[List[ProjectFilmingDayInput]] = None
    episode_metadata: Optional[List[ProjectEpisodeInput]] = None


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
    return _serialize_project_list(session, projects)


@router.get("/admin/count", dependencies=[Depends(require_admin)])
def count_admin_projects(
    category: Optional[str] = None,
    session: Session = Depends(get_session),
):
    query = select(func.count(Project.id))
    if category:
        query = query.where(Project.category == category.strip().lower())
    return {"count": session.exec(query).one()}


@router.get("/admin/{project_ref}", dependencies=[Depends(require_admin)])
def get_admin_project(project_ref: str, session: Session = Depends(get_session)):
    p = _get_project_by_ref(session, project_ref)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"project": _serialize_project(session, p)}


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
    return _serialize_project_list(session, projects)


# ----------------------------
# GET one
# ----------------------------

@router.get("/{project_ref}")
def get_project(project_ref: str, session: Session = Depends(get_session)):
    p = _get_project_by_ref(session, project_ref)
    if not p or not p.is_visible:
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

    slug = _normalize_slug(payload.slug)
    if slug:
        existing = session.exec(select(Project).where(Project.slug == slug)).first()
        if existing:
            raise HTTPException(status_code=400, detail="Project slug already exists")

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
        hashtag=_clean_hashtag(payload.hashtag),
        slug=slug,
        category=category,
        thumbnail_url=(payload.thumbnail_url.strip() if payload.thumbnail_url else None),
        thumbnail_focal_x=payload.thumbnail_focal_x,
        thumbnail_focal_y=payload.thumbnail_focal_y,
        is_visible=payload.is_visible,
        year=payload.year,
        episode_count=(payload.episode_count if category == "series" and payload.episode_count and payload.episode_count > 0 else None),
        description=(payload.description.strip() if payload.description else None),
        playlists_json=json.dumps(playlist_objs),
        announcement_url=(payload.announcement_url.strip() if payload.announcement_url else None),
        tweet_url=(payload.tweet_url.strip() if payload.tweet_url else None),
        tweet_label=(payload.tweet_label.strip() if payload.tweet_label else None),
        youtube_url=(payload.youtube_url.strip() if payload.youtube_url else None),
        youtube_label=(payload.youtube_label.strip() if payload.youtube_label else None),
        mydramalist_url=(payload.mydramalist_url.strip() if payload.mydramalist_url else None),
        gmmtv_url=(payload.gmmtv_url.strip() if payload.gmmtv_url else None),
        official_twitter_url=(payload.official_twitter_url.strip() if payload.official_twitter_url else None),
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
    if category == "series":
        _replace_series_metadata(session, p.id, payload.filming_days, payload.episode_metadata)
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
        if cat != "series":
            p.episode_count = None

    if payload.slug is not None:
        slug = _normalize_slug(payload.slug)
        if slug:
            existing = session.exec(select(Project).where(Project.slug == slug, Project.id != project_id)).first()
            if existing:
                raise HTTPException(status_code=400, detail="Project slug already exists")
        p.slug = slug

    if payload.original_title is not None:
        p.original_title = payload.original_title.strip() or None
    if payload.hashtag is not None:
        p.hashtag = _clean_hashtag(payload.hashtag)
    if payload.thumbnail_url is not None:
        p.thumbnail_url = payload.thumbnail_url.strip() or None
    if payload.thumbnail_focal_x is not None:
        p.thumbnail_focal_x = payload.thumbnail_focal_x
    if payload.thumbnail_focal_y is not None:
        p.thumbnail_focal_y = payload.thumbnail_focal_y
    if payload.is_visible is not None:
        p.is_visible = payload.is_visible
    if payload.year is not None:
        p.year = payload.year
    if payload.episode_count is not None:
        p.episode_count = payload.episode_count if payload.episode_count > 0 else None
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
    if payload.tweet_label is not None:
        p.tweet_label = payload.tweet_label.strip() or None
    if payload.youtube_url is not None:
        p.youtube_url = payload.youtube_url.strip() or None
    if payload.youtube_label is not None:
        p.youtube_label = payload.youtube_label.strip() or None
    if payload.mydramalist_url is not None:
        p.mydramalist_url = payload.mydramalist_url.strip() or None
    if payload.gmmtv_url is not None:
        p.gmmtv_url = payload.gmmtv_url.strip() or None
    if payload.official_twitter_url is not None:
        p.official_twitter_url = payload.official_twitter_url.strip() or None
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

    metadata_was_sent = payload.filming_days is not None or payload.episode_metadata is not None
    if metadata_was_sent or p.category != "series":
        _replace_series_metadata(
            session,
            project_id,
            payload.filming_days if p.category == "series" else [],
            payload.episode_metadata if p.category == "series" else [],
        )
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
