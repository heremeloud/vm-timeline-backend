import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import update
from sqlmodel import Session, select

from database import get_session
from middleware.auth import require_admin
from models import Event, EventCategoryOption, EventSubcategoryOption, EventView


router = APIRouter(prefix="/event-categories", tags=["Event Categories"])


class CategoryCreate(BaseModel):
    name: str
    label: Optional[str] = None


class CategoryUpdate(BaseModel):
    name: Optional[str] = None
    label: Optional[str] = None
    sort_order: Optional[int] = None
    is_default: Optional[bool] = None


class SubcategoryCreate(BaseModel):
    category_id: int
    name: str
    label: Optional[str] = None


class SubcategoryUpdate(BaseModel):
    name: Optional[str] = None
    label: Optional[str] = None
    sort_order: Optional[int] = None


def normalize_option_name(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def default_label(value: str) -> str:
    return " ".join(part.upper() if part == "gmmtv" else part.capitalize() for part in value.split())


def category_config(session: Session):
    categories = session.exec(
        select(EventCategoryOption).order_by(EventCategoryOption.sort_order, EventCategoryOption.id)
    ).all()
    subcategories = session.exec(
        select(EventSubcategoryOption).order_by(EventSubcategoryOption.sort_order, EventSubcategoryOption.id)
    ).all()
    children = {}
    for subcategory in subcategories:
        children.setdefault(subcategory.category_id, []).append({
            "id": subcategory.id,
            "value": subcategory.name,
            "label": subcategory.label,
            "sort_order": subcategory.sort_order,
        })
    return [{
        "id": category.id,
        "value": category.name,
        "label": category.label,
        "sort_order": category.sort_order,
        "is_default": category.is_default,
        "subcategories": children.get(category.id, []),
    } for category in categories]


@router.get("")
def list_event_categories(session: Session = Depends(get_session)):
    return category_config(session)


@router.post("/admin", dependencies=[Depends(require_admin)])
def create_category(payload: CategoryCreate, session: Session = Depends(get_session)):
    name = normalize_option_name(payload.name)
    if not name:
        raise HTTPException(status_code=400, detail="Category name is required")
    if session.exec(select(EventCategoryOption).where(EventCategoryOption.name == name)).first():
        raise HTTPException(status_code=400, detail="Category already exists")
    last = session.exec(select(EventCategoryOption).order_by(EventCategoryOption.sort_order.desc())).first()
    has_default = session.exec(select(EventCategoryOption).where(EventCategoryOption.is_default == True)).first()
    category = EventCategoryOption(
        name=name,
        label=(payload.label or "").strip() or default_label(name),
        sort_order=(last.sort_order + 1) if last else 0,
        is_default=has_default is None,
    )
    session.add(category)
    session.commit()
    session.refresh(category)
    return category


@router.patch("/admin/{category_id}", dependencies=[Depends(require_admin)])
def update_category(category_id: int, payload: CategoryUpdate, session: Session = Depends(get_session)):
    category = session.get(EventCategoryOption, category_id)
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")
    old_name = category.name
    if payload.name is not None:
        name = normalize_option_name(payload.name)
        if not name:
            raise HTTPException(status_code=400, detail="Category name cannot be empty")
        duplicate = session.exec(select(EventCategoryOption).where(
            EventCategoryOption.name == name, EventCategoryOption.id != category_id
        )).first()
        if duplicate:
            raise HTTPException(status_code=400, detail="Category already exists")
        category.name = name
        if name != old_name:
            session.exec(update(Event).where(Event.category == old_name).values(category=name))
            session.exec(update(EventView).where(EventView.category == old_name).values(category=name))
    if payload.label is not None:
        category.label = payload.label.strip() or default_label(category.name)
    if payload.sort_order is not None:
        category.sort_order = payload.sort_order
    if payload.is_default is True:
        session.exec(update(EventCategoryOption).values(is_default=False))
        category.is_default = True
    elif payload.is_default is False and category.is_default:
        raise HTTPException(status_code=400, detail="Choose another category to replace the current default")
    session.add(category)
    session.commit()
    session.refresh(category)
    return category


@router.delete("/admin/{category_id}", dependencies=[Depends(require_admin)])
def delete_category(category_id: int, session: Session = Depends(get_session)):
    category = session.get(EventCategoryOption, category_id)
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")
    if len(session.exec(select(EventCategoryOption)).all()) <= 1:
        raise HTTPException(status_code=400, detail="At least one event category is required")
    if session.exec(select(Event).where(Event.category == category.name)).first() or session.exec(
        select(EventView).where(EventView.category == category.name)
    ).first():
        raise HTTPException(status_code=400, detail="Category is in use and cannot be deleted")
    for subcategory in session.exec(select(EventSubcategoryOption).where(
        EventSubcategoryOption.category_id == category_id
    )).all():
        session.delete(subcategory)
    was_default = category.is_default
    session.delete(category)
    session.flush()
    if was_default:
        replacement = session.exec(
            select(EventCategoryOption).order_by(EventCategoryOption.sort_order, EventCategoryOption.id)
        ).first()
        if replacement:
            replacement.is_default = True
            session.add(replacement)
    session.commit()
    return {"status": "deleted", "id": category_id}


@router.post("/admin/subcategories", dependencies=[Depends(require_admin)])
def create_subcategory(payload: SubcategoryCreate, session: Session = Depends(get_session)):
    category = session.get(EventCategoryOption, payload.category_id)
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")
    name = normalize_option_name(payload.name)
    if not name:
        raise HTTPException(status_code=400, detail="Subcategory name is required")
    duplicate = session.exec(select(EventSubcategoryOption).where(
        EventSubcategoryOption.category_id == payload.category_id,
        EventSubcategoryOption.name == name,
    )).first()
    if duplicate:
        raise HTTPException(status_code=400, detail="Subcategory already exists in this category")
    last = session.exec(select(EventSubcategoryOption).where(
        EventSubcategoryOption.category_id == payload.category_id
    ).order_by(EventSubcategoryOption.sort_order.desc())).first()
    subcategory = EventSubcategoryOption(
        category_id=payload.category_id,
        name=name,
        label=(payload.label or "").strip() or default_label(name),
        sort_order=(last.sort_order + 1) if last else 0,
    )
    session.add(subcategory)
    session.commit()
    session.refresh(subcategory)
    return subcategory


@router.patch("/admin/subcategories/{subcategory_id}", dependencies=[Depends(require_admin)])
def update_subcategory(
    subcategory_id: int,
    payload: SubcategoryUpdate,
    session: Session = Depends(get_session),
):
    subcategory = session.get(EventSubcategoryOption, subcategory_id)
    if not subcategory:
        raise HTTPException(status_code=404, detail="Subcategory not found")
    category = session.get(EventCategoryOption, subcategory.category_id)
    old_name = subcategory.name
    if payload.name is not None:
        name = normalize_option_name(payload.name)
        if not name:
            raise HTTPException(status_code=400, detail="Subcategory name cannot be empty")
        duplicate = session.exec(select(EventSubcategoryOption).where(
            EventSubcategoryOption.category_id == subcategory.category_id,
            EventSubcategoryOption.name == name,
            EventSubcategoryOption.id != subcategory_id,
        )).first()
        if duplicate:
            raise HTTPException(status_code=400, detail="Subcategory already exists in this category")
        subcategory.name = name
        if name != old_name:
            session.exec(update(Event).where(
                Event.category == category.name, Event.subcategory == old_name
            ).values(subcategory=name))
            session.exec(update(EventView).where(
                EventView.category == category.name, EventView.subcategory == old_name
            ).values(subcategory=name))
    if payload.label is not None:
        subcategory.label = payload.label.strip() or default_label(subcategory.name)
    if payload.sort_order is not None:
        subcategory.sort_order = payload.sort_order
    session.add(subcategory)
    session.commit()
    session.refresh(subcategory)
    return subcategory


@router.delete("/admin/subcategories/{subcategory_id}", dependencies=[Depends(require_admin)])
def delete_subcategory(subcategory_id: int, session: Session = Depends(get_session)):
    subcategory = session.get(EventSubcategoryOption, subcategory_id)
    if not subcategory:
        raise HTTPException(status_code=404, detail="Subcategory not found")
    category = session.get(EventCategoryOption, subcategory.category_id)
    if session.exec(select(Event).where(
        Event.category == category.name, Event.subcategory == subcategory.name
    )).first() or session.exec(select(EventView).where(
        EventView.category == category.name, EventView.subcategory == subcategory.name
    )).first():
        raise HTTPException(status_code=400, detail="Subcategory is in use and cannot be deleted")
    session.delete(subcategory)
    session.commit()
    return {"status": "deleted", "id": subcategory_id}
