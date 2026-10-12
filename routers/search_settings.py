import json
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from database import get_session
from middleware.auth import require_admin
from models import SearchSettings
from search_rules import DEFAULT_IDENTITY_TAGS, DEFAULT_TYPO_TOLERANCE, TYPO_THRESHOLDS, normalize_tag, stored_identity_tags

router = APIRouter(prefix="/search-settings", tags=["Search Settings"])

MAX_IDENTITY_TAGS = 50
MAX_TAG_LENGTH = 60


class SearchSettingsPayload(BaseModel):
    identity_tags: List[str]
    typo_tolerance: str


def _serialize(settings: SearchSettings | None) -> dict:
    return {
        "identity_tags": stored_identity_tags(settings),
        "typo_tolerance": settings.typo_tolerance if settings else DEFAULT_TYPO_TOLERANCE,
        "default_identity_tags": list(DEFAULT_IDENTITY_TAGS),
        "typo_tolerance_options": list(TYPO_THRESHOLDS),
    }


@router.get("")
def get_search_settings(session: Session = Depends(get_session)):
    """Public: the frontend needs the identity tags to skip them when linking hashtags to events."""
    return _serialize(session.get(SearchSettings, 1))


@router.put("", dependencies=[Depends(require_admin)])
def update_search_settings(payload: SearchSettingsPayload, session: Session = Depends(get_session)):
    if payload.typo_tolerance not in TYPO_THRESHOLDS:
        raise HTTPException(status_code=400, detail=f"typo_tolerance must be one of: {', '.join(TYPO_THRESHOLDS)}")

    tags: list[str] = []
    seen: set[str] = set()
    for raw in payload.identity_tags:
        tag = raw.strip().lstrip("#").strip()
        if not tag or normalize_tag(tag) in seen:
            continue
        if len(tag) > MAX_TAG_LENGTH:
            raise HTTPException(status_code=400, detail=f"A hashtag can be at most {MAX_TAG_LENGTH} characters.")
        seen.add(normalize_tag(tag))
        tags.append(tag)
    if len(tags) > MAX_IDENTITY_TAGS:
        raise HTTPException(status_code=400, detail=f"At most {MAX_IDENTITY_TAGS} identity hashtags are allowed.")

    settings = session.get(SearchSettings, 1) or SearchSettings(id=1)
    settings.identity_tags_json = json.dumps(tags, ensure_ascii=False)
    settings.typo_tolerance = payload.typo_tolerance
    session.add(settings)
    session.commit()
    session.refresh(settings)
    return _serialize(settings)
