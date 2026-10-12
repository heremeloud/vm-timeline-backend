"""Event and project search rules (documented in vm-timeline-frontend/docs/search-rules.md).

Safety: the user's text never reaches SQL. Matching happens in Python over a few text columns and the result is
applied to the query as `id IN (...)` with bound parameters. Input is length-capped.

Matching is per word: every word typed must be found somewhere in the item, in any order. With typo tolerance on, a
word also matches a close word of the item (difflib, no extra dependency).

Identity hashtags (ViewMim, วิวมิ้ม, …) are on nearly every event, so they are ignored when matching tags and date
hashtags; otherwise a search for "mim" would return everything. They still match in names and keywords.
The list and the tolerance are admin-editable (SearchSettings, Manage Display → Event Setup).
"""
import difflib
import json
import re
from typing import Callable, Iterable, Optional

from sqlmodel import Session, select

from models import Event, Project, SearchSettings

MAX_SEARCH_LENGTH = 100
MAX_SEARCH_WORDS = 8
MIN_FUZZY_WORD_LENGTH = 4

DEFAULT_IDENTITY_TAGS = (
    "ViewMim",
    "วิวมิ้ม",
    "VIMMY",
    "viewbenyapa",
    "วิวเบญญาภา",
    "สระอิของวว",
    "mimrattanawadee",
    "มิ้มรัตนวดี",
    "ด้อมเป็ดจิ๋ว",
)

# similarity a word needs to count as a typo of another word; None turns fuzzy matching off
TYPO_THRESHOLDS: dict[str, Optional[float]] = {"off": None, "normal": 0.8, "loose": 0.7}
DEFAULT_TYPO_TOLERANCE = "normal"


def normalize_tag(tag: str) -> str:
    return tag.strip().lstrip("#").strip().lower()


class SearchRules:
    """Identity tags (normalized) and the typo threshold used for one search."""

    def __init__(self, identity_tags: Iterable[str], typo_tolerance: str):
        self.identity_tags = frozenset(normalize_tag(tag) for tag in identity_tags if normalize_tag(tag))
        self.typo_threshold = TYPO_THRESHOLDS.get(typo_tolerance, TYPO_THRESHOLDS[DEFAULT_TYPO_TOLERANCE])

    def is_identity(self, tag: str) -> bool:
        return normalize_tag(tag) in self.identity_tags


def stored_identity_tags(settings: Optional[SearchSettings]) -> list[str]:
    if settings is None or settings.identity_tags_json is None:
        return list(DEFAULT_IDENTITY_TAGS)
    return [tag for tag in _load_json_list(settings.identity_tags_json) if isinstance(tag, str)]


def load_rules(session: Session) -> SearchRules:
    settings = session.get(SearchSettings, 1)
    return SearchRules(
        stored_identity_tags(settings),
        settings.typo_tolerance if settings else DEFAULT_TYPO_TOLERANCE,
    )


def search_words(raw_term: Optional[str]) -> list[str]:
    """The cleaned, length-capped words of a search string, lowercased and without a leading '#'."""
    cleaned = (raw_term or "").strip()[:MAX_SEARCH_LENGTH]
    words = [word.lstrip("#").lower() or word.lower() for word in cleaned.split()]
    return words[:MAX_SEARCH_WORDS]


def _load_json_list(value: Optional[str]) -> list:
    try:
        data = json.loads(value or "[]")
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _event_text(event, rules: SearchRules) -> str:
    """Lowercased event text; identity hashtags are left out of tags and date hashtags."""
    parts = [event.name, event.english_name, event.keyword]
    for tag in _load_json_list(event.tags_json):
        if isinstance(tag, (str, int, float)) and not rules.is_identity(str(tag)):
            parts.append(str(tag))
    for item in _load_json_list(event.date_items_json):
        if not isinstance(item, dict):
            continue
        parts.append(str(item.get("date") or ""))
        parts.append(str(item.get("keyword") or ""))
        hashtag = str(item.get("hashtag") or "")
        if not rules.is_identity(hashtag):
            parts.append(hashtag)
    return " ".join(part for part in parts if part).lower()


def _project_text(project, rules: SearchRules) -> str:
    parts = [project.title, project.original_title, project.category]
    if project.hashtag and not rules.is_identity(project.hashtag):
        parts.append(project.hashtag)
    return " ".join(part for part in parts if part).lower()


def _is_close(word: str, vocabulary: set[str], threshold: Optional[float]) -> bool:
    if threshold is None or len(word) < MIN_FUZZY_WORD_LENGTH:
        return False
    return any(
        abs(len(word) - len(candidate)) <= 2
        and difflib.SequenceMatcher(None, word, candidate).ratio() >= threshold
        for candidate in vocabulary
    )


def _matching_ids(rows, words: list[str], rules: SearchRules, text_of: Callable) -> list[int]:
    ids = []
    for row in rows:
        text = text_of(row, rules)
        vocabulary = set(re.findall(r"\w+", text))
        if all(word in text or _is_close(word, vocabulary, rules.typo_threshold) for word in words):
            ids.append(row.id)
    return ids


def event_search_ids(session: Session, raw_term: Optional[str]) -> Optional[list[int]]:
    """Ids of events matching every word, or None when there is nothing to search for."""
    words = search_words(raw_term)
    if not words:
        return None
    rows = session.exec(select(
        Event.id, Event.name, Event.english_name, Event.keyword, Event.tags_json, Event.date_items_json,
    )).all()
    return _matching_ids(rows, words, load_rules(session), _event_text)


def project_search_ids(session: Session, raw_term: Optional[str]) -> Optional[list[int]]:
    words = search_words(raw_term)
    if not words:
        return None
    rows = session.exec(select(
        Project.id, Project.title, Project.original_title, Project.category, Project.hashtag,
    )).all()
    return _matching_ids(rows, words, load_rules(session), _project_text)


def event_search_filter(session: Session, raw_term: Optional[str]):
    """A WHERE clause for the search text, or None when there is nothing to search for."""
    ids = event_search_ids(session, raw_term)
    return None if ids is None else Event.id.in_(ids)


def project_search_filter(session: Session, raw_term: Optional[str]):
    ids = project_search_ids(session, raw_term)
    return None if ids is None else Project.id.in_(ids)
