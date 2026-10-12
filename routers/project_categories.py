import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, update
from sqlmodel import Session, select

from constants import PROJECT_CATEGORIES
from database import get_session
from middleware.auth import require_admin
from models import Project, ProjectCategoryOption


router = APIRouter(prefix="/project-categories", tags=["Project Categories"])

# Hiding only removes a category from the public Projects filter; its projects stay listed and admins can still pick it.
# Project behaviour (episodes, filming days, relationship chart) is keyed on this category, so it cannot change.
PROTECTED_CATEGORY = "series"


class ProjectCategoryCreate(BaseModel):
    name: str
    label: Optional[str] = None


class ProjectCategoryUpdate(BaseModel):
    name: Optional[str] = None
    label: Optional[str] = None
    sort_order: Optional[int] = None
    is_visible: Optional[bool] = None


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _ordered(session: Session) -> list[ProjectCategoryOption]:
    return list(session.exec(
        select(ProjectCategoryOption).order_by(ProjectCategoryOption.sort_order, ProjectCategoryOption.id)
    ).all())


def project_category_names(session: Session) -> list[str]:
    """Category names in their admin-chosen order (the built-in list if none are saved yet)."""
    return [category.name for category in _ordered(session)] or list(PROJECT_CATEGORIES)


def _serialize(session: Session, category: ProjectCategoryOption) -> dict:
    count = session.exec(select(func.count(Project.id)).where(Project.category == category.name)).one()
    return {
        "id": category.id,
        "value": category.name,
        "label": category.label,
        "sort_order": category.sort_order,
        "is_visible": category.is_visible is not False,
        "project_count": count,
        "is_protected": category.name == PROTECTED_CATEGORY,
    }


@router.get("")
def list_project_categories(session: Session = Depends(get_session)):
    return [_serialize(session, category) for category in _ordered(session)]


@router.post("/admin", dependencies=[Depends(require_admin)])
def create_project_category(payload: ProjectCategoryCreate, session: Session = Depends(get_session)):
    name = normalize_name(payload.name)
    if not name:
        raise HTTPException(status_code=400, detail="Category name is required")
    if session.exec(select(ProjectCategoryOption).where(ProjectCategoryOption.name == name)).first():
        raise HTTPException(status_code=400, detail="Category already exists")
    last = session.exec(select(ProjectCategoryOption).order_by(ProjectCategoryOption.sort_order.desc())).first()
    category = ProjectCategoryOption(
        name=name,
        label=(payload.label or "").strip() or name.title(),
        sort_order=(last.sort_order + 1) if last else 0,
    )
    session.add(category)
    session.commit()
    session.refresh(category)
    return _serialize(session, category)


@router.patch("/admin/{category_id}", dependencies=[Depends(require_admin)])
def update_project_category(category_id: int, payload: ProjectCategoryUpdate, session: Session = Depends(get_session)):
    category = session.get(ProjectCategoryOption, category_id)
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")
    if payload.name is not None:
        name = normalize_name(payload.name)
        if not name:
            raise HTTPException(status_code=400, detail="Category name cannot be empty")
        if name != category.name:
            if category.name == PROTECTED_CATEGORY:
                raise HTTPException(status_code=400, detail="The series category cannot be renamed; change its label instead")
            if session.exec(select(ProjectCategoryOption).where(
                ProjectCategoryOption.name == name, ProjectCategoryOption.id != category_id
            )).first():
                raise HTTPException(status_code=400, detail="Category already exists")
            session.exec(update(Project).where(Project.category == category.name).values(category=name))
            category.name = name
    if payload.label is not None:
        category.label = payload.label.strip() or category.name.title()
    if payload.sort_order is not None:
        category.sort_order = payload.sort_order
    if payload.is_visible is not None:
        category.is_visible = payload.is_visible
    session.add(category)
    session.commit()
    session.refresh(category)
    return _serialize(session, category)


@router.delete("/admin/{category_id}", dependencies=[Depends(require_admin)])
def delete_project_category(category_id: int, session: Session = Depends(get_session)):
    category = session.get(ProjectCategoryOption, category_id)
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")
    if category.name == PROTECTED_CATEGORY:
        raise HTTPException(status_code=400, detail="The series category cannot be deleted")
    in_use = session.exec(select(func.count(Project.id)).where(Project.category == category.name)).one()
    if in_use:
        raise HTTPException(status_code=400, detail=f"{in_use} project(s) use this category; move them to another category first")
    session.delete(category)
    session.commit()
    return {"status": "deleted"}
