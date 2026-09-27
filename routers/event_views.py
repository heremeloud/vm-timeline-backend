import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from constants import EVENT_CATEGORIES, EVENT_SUBCATEGORIES
from database import get_session
from middleware.auth import require_admin
from models import EventCategoryOption, EventSubcategoryOption, EventView


router = APIRouter(prefix="/event-views", tags=["Event Views"])

VALID_AUTHORS = {"viewmim", "view", "mim", "vimmy"}
VALID_SORTS = {"newest", "oldest"}
VALID_VIEW_MODES = {"list", "calendar"}


class EventViewCreate(BaseModel):
    title: str
    slug: str
    is_visible: bool = True
    name_filter: Optional[str] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    author: Optional[str] = None
    event_sort: str = "newest"
    view_mode: str = "list"


class EventViewUpdate(BaseModel):
    title: Optional[str] = None
    slug: Optional[str] = None
    is_visible: Optional[bool] = None
    name_filter: Optional[str] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    author: Optional[str] = None
    event_sort: Optional[str] = None
    view_mode: Optional[str] = None
    sort_order: Optional[int] = None


def _normalize_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    if not slug:
        raise HTTPException(status_code=400, detail="Slug is required")
    return slug


def _clean_optional(value: Optional[str]) -> Optional[str]:
    return value.strip() or None if value else None


def _validate_filters(session, category, subcategory, author, event_sort, view_mode):
    category = _clean_optional(category)
    subcategory = _clean_optional(subcategory)
    author = _clean_optional(author)
    categories = session.exec(select(EventCategoryOption)).all()
    valid_categories = {item.name for item in categories} or set(EVENT_CATEGORIES)
    if categories:
        category_by_id = {item.id: item.name for item in categories}
        valid_subcategories = {item.name: set() for item in categories}
        for item in session.exec(select(EventSubcategoryOption)).all():
            category_name = category_by_id.get(item.category_id)
            if category_name:
                valid_subcategories[category_name].add(item.name)
    else:
        valid_subcategories = {key: set(values) for key, values in EVENT_SUBCATEGORIES.items()}
    if category and category not in valid_categories:
        raise HTTPException(status_code=400, detail="Invalid event category")
    if subcategory and subcategory not in valid_subcategories.get(category, set()):
        raise HTTPException(status_code=400, detail="Invalid event subcategory")
    if author and author not in VALID_AUTHORS:
        raise HTTPException(status_code=400, detail="Invalid event artist filter")
    if event_sort not in VALID_SORTS:
        raise HTTPException(status_code=400, detail="Invalid event sort")
    if view_mode not in VALID_VIEW_MODES:
        raise HTTPException(status_code=400, detail="Invalid event view mode")
    return category, subcategory, author


@router.get("/admin", dependencies=[Depends(require_admin)])
def list_admin_event_views(session: Session = Depends(get_session)):
    return session.exec(select(EventView).order_by(EventView.sort_order, EventView.id)).all()


@router.get("")
def list_public_event_views(session: Session = Depends(get_session)):
    return session.exec(
        select(EventView)
        .where(EventView.is_visible == True)
        .order_by(EventView.sort_order, EventView.id)
    ).all()


@router.post("/admin", dependencies=[Depends(require_admin)])
def create_event_view(payload: EventViewCreate, session: Session = Depends(get_session)):
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Page name is required")
    slug = _normalize_slug(payload.slug)
    if session.exec(select(EventView).where(EventView.slug == slug)).first():
        raise HTTPException(status_code=400, detail="Event page slug already exists")
    category, subcategory, author = _validate_filters(
        session, payload.category, payload.subcategory, payload.author, payload.event_sort, payload.view_mode
    )
    last = session.exec(select(EventView).order_by(EventView.sort_order.desc(), EventView.id.desc())).first()
    event_view = EventView(
        title=title,
        slug=slug,
        is_visible=payload.is_visible,
        name_filter=_clean_optional(payload.name_filter),
        category=category,
        subcategory=subcategory,
        author=author,
        event_sort=payload.event_sort,
        view_mode=payload.view_mode,
        sort_order=(last.sort_order + 1) if last else 0,
    )
    session.add(event_view)
    session.commit()
    session.refresh(event_view)
    return event_view


@router.patch("/admin/{event_view_id}", dependencies=[Depends(require_admin)])
def update_event_view(
    event_view_id: int,
    payload: EventViewUpdate,
    session: Session = Depends(get_session),
):
    event_view = session.get(EventView, event_view_id)
    if not event_view:
        raise HTTPException(status_code=404, detail="Filtered event page not found")

    changes = payload.model_dump(exclude_unset=True)
    if "title" in changes:
        title = (changes["title"] or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="Page name cannot be empty")
        event_view.title = title
    if "slug" in changes:
        slug = _normalize_slug(changes["slug"])
        existing = session.exec(
            select(EventView).where(EventView.slug == slug, EventView.id != event_view_id)
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="Event page slug already exists")
        event_view.slug = slug

    next_category = changes.get("category", event_view.category)
    next_subcategory = changes.get("subcategory", event_view.subcategory)
    next_author = changes.get("author", event_view.author)
    next_sort = changes.get("event_sort", event_view.event_sort)
    next_mode = changes.get("view_mode", event_view.view_mode)
    category, subcategory, author = _validate_filters(
        session, next_category, next_subcategory, next_author, next_sort, next_mode
    )
    event_view.category = category
    event_view.subcategory = subcategory
    event_view.author = author
    event_view.event_sort = next_sort
    event_view.view_mode = next_mode

    if "name_filter" in changes:
        event_view.name_filter = _clean_optional(changes["name_filter"])
    if "is_visible" in changes:
        event_view.is_visible = changes["is_visible"]
    if "sort_order" in changes:
        event_view.sort_order = changes["sort_order"]

    session.add(event_view)
    session.commit()
    session.refresh(event_view)
    return event_view


@router.delete("/admin/{event_view_id}", dependencies=[Depends(require_admin)])
def delete_event_view(event_view_id: int, session: Session = Depends(get_session)):
    event_view = session.get(EventView, event_view_id)
    if not event_view:
        raise HTTPException(status_code=404, detail="Filtered event page not found")
    session.delete(event_view)
    session.commit()
    return {"status": "deleted", "id": event_view_id}


@router.get("/{slug}")
def get_event_view(slug: str, session: Session = Depends(get_session)):
    event_view = session.exec(
        select(EventView).where(EventView.slug == slug.strip().lower(), EventView.is_visible == True)
    ).first()
    if not event_view:
        raise HTTPException(status_code=404, detail="Filtered event page not found")
    return event_view
