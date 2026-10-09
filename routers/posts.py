import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import case, func, or_, true
from sqlalchemy.orm import aliased
from sqlmodel import Session, SQLModel, select, desc
from database import get_session
from models import Post, PostText, Author, Event, Project, ProjectFilmingDay, ProjectEpisode, ProjectFittingWorkshop
from middleware.auth import is_admin_token, require_admin
from instagram_archive import (
    InstagramArchiveError,
    InstagramArchiveRateLimitError,
    InstagramArchiveSessionError,
    InstagramPostBrowser,
    archive_cookie,
    extension_for_media,
    instagram_shortcode,
)
from routers import media as media_router

router = APIRouter(prefix="/posts", tags=["Posts"])


def _has_public_author():
    """A post is attributable through a visible saved author or a local author."""
    return or_(
        Author.show_on_timeline == True,
        func.trim(func.coalesce(Post.temp_author_name, "")) != "",
    )


def _normalize_post_author(post: Post) -> None:
    """Keep saved-author and post-local-author storage mutually exclusive."""
    post.temp_author_name = (post.temp_author_name or "").strip() or None
    post.temp_author_pfp_url = (
        (post.temp_author_pfp_url or "").strip() or None
        if post.temp_author_name
        else None
    )
    if post.temp_author_name:
        post.author_id = None
    elif post.author_id is None:
        raise HTTPException(
            status_code=422,
            detail="Select an author or provide temp_author_name",
        )


def _normalize_display_source(post: Post) -> None:
    post.display_source = (post.display_source or "external").strip().lower()
    if post.display_source not in {"external", "r2"}:
        raise HTTPException(status_code=422, detail="display_source must be 'external' or 'r2'")


def _hide_author_categories(query, hidden: str | None):
    """Drop posts by saved authors in the comma-separated categories.

    The pseudo-category `temp` drops posts with a one-off local author (no saved author) instead.
    """
    categories = [item.strip() for item in (hidden or "").split(",") if item.strip()]
    if not categories:
        return query
    saved = [item for item in categories if item != "temp"]
    query = query.outerjoin(Author, Author.id == Post.author_id)
    if "temp" in categories:
        query = query.where(Post.author_id != None)
    if saved:
        query = query.where(or_(Post.author_id == None, Author.category.not_in(saved)))
    return query


def _filter_admin_author(query, author_filter: str | None):
    if author_filter is None or author_filter == "all":
        return query
    if author_filter == "temp":
        return query.where(
            Post.author_id == None,
            func.trim(func.coalesce(Post.temp_author_name, "")) != "",
        )
    try:
        saved_author_id = int(author_filter)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid author filter") from exc
    return query.where(Post.author_id == saved_author_id)


def _normalize_utc_timestamp(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="posted_at_utc must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise HTTPException(status_code=422, detail="posted_at_utc must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validate_reply_timing(reply_date: str | None, reply_utc: str | None, parent: Post) -> None:
    if reply_date and parent.posted_at and reply_date < parent.posted_at:
        raise HTTPException(status_code=422, detail="A reply cannot be dated before its parent post")
    if reply_utc and parent.posted_at_utc:
        reply_time = datetime.fromisoformat(reply_utc.replace("Z", "+00:00"))
        parent_time = datetime.fromisoformat(parent.posted_at_utc.replace("Z", "+00:00"))
        if reply_time <= parent_time:
            raise HTTPException(status_code=422, detail="A reply's exact time must be later than its parent post")


class PostReorder(SQLModel):
    target_post_id: int
    position: str


class PostArchiveRequest(SQLModel):
    destination: str = "primary"


def _filter_post_platform(query, platform: str | None):
    if not platform or platform == "all":
        return query
    if platform == "bc":
        return query.where(Post.platform == "ig", Post.content_type == "broadcast")
    if platform == "igs":
        return query.where(Post.platform == "ig", Post.content_type == "story")
    if platform == "ig-post":
        return query.where(Post.platform == "ig", Post.content_type == "post")
    return query.where(Post.platform == platform)


def _related_page_filter(include_hidden: bool, authorization: str | None):
    """Related lists show posts ticked "Show post on related page", even if hidden from the public timeline
    (`is_visible` is deliberately not checked; a hidden author still hides the post).

    The admin can ask for every linked post (`include_hidden`) to see what links where
    regardless of that checkbox; this needs a valid admin token.
    """
    if not include_hidden:
        return Post.show_on_related_page == True
    token = (authorization if isinstance(authorization, str) else "").removeprefix("Bearer ").strip()
    if not token or not is_admin_token(token):
        raise HTTPException(status_code=403, detail="Admin only")
    return true()


PROJECT_ENTRY_TYPES = ("filming", "episodes", "fitting", "workshop", "prep")


def _normalize_project_entry_links(value) -> str:
    """Canonical JSON for a post's explicit project-row links, so SQL LIKE can match them exactly.

    Accepts a JSON string or a list of {project_id, entry_type, entry_number}; invalid items are dropped.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except (TypeError, ValueError):
            value = []
    links = {}
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            project_id = int(item.get("project_id"))
            entry_number = int(item.get("entry_number"))
        except (TypeError, ValueError):
            continue
        entry_type = str(item.get("entry_type") or "").strip().lower()
        if entry_type not in PROJECT_ENTRY_TYPES or entry_number < 0:
            continue
        links[(project_id, entry_type, entry_number)] = {
            "entry_number": entry_number,
            "entry_type": entry_type,
            "project_id": project_id,
        }
    ordered = [links[key] for key in sorted(links)]
    return json.dumps(ordered, sort_keys=True, separators=(",", ":"))


def _links_from_json(links_json: str | None) -> set[tuple[int, str, int]]:
    try:
        items = json.loads(links_json or "[]")
    except (TypeError, ValueError):
        return set()
    return {
        (item["project_id"], item["entry_type"], item["entry_number"])
        for item in items
        if isinstance(item, dict) and {"project_id", "entry_type", "entry_number"} <= item.keys()
    }


def _post_project_entry_links(post: Post) -> set[tuple[int, str, int]]:
    return _links_from_json(post.project_entry_links_json)


def _project_entry_link_like(project_id: int, entry_type: str | None = None, entry_number: int | None = None) -> str:
    """LIKE pattern matching the canonical JSON of one project row (or any row of the project)."""
    if entry_type is None or entry_number is None:
        return f'%"project_id":{project_id}}}%'
    return f'%"entry_number":{entry_number},"entry_type":"{entry_type}","project_id":{project_id}}}%'


def _project_entry_rows(session: Session, project_id: int) -> list[tuple[str, int, str | None]]:
    """Every Q day, episode, fitting and workshop row of a project as (entry_type, number, hashtag)."""
    rows: list[tuple[str, int, str | None]] = []
    for row in session.exec(select(ProjectFilmingDay).where(ProjectFilmingDay.project_id == project_id)).all():
        rows.append(("filming", row.q_number, row.hashtag))
    for row in session.exec(select(ProjectEpisode).where(ProjectEpisode.project_id == project_id)).all():
        rows.append(("episodes", row.episode_number, row.hashtag))
    for row in session.exec(select(ProjectFittingWorkshop).where(ProjectFittingWorkshop.project_id == project_id)).all():
        rows.append((row.kind, row.number, row.hashtag))
    return rows


_HASHTAG_PATTERN = re.compile(r"#([\w]+)", flags=re.UNICODE)


def _hashtags_in_text(*parts: str | None) -> set[str]:
    """Casefolded hashtags (without #) found in any of the given texts."""
    text = "\n".join(part for part in parts if part)
    return {match.casefold() for match in _HASHTAG_PATTERN.findall(text)}


def _hashtags_in_post(post: Post) -> set[str]:
    return _hashtags_in_text(post.caption, post.caption_translation, post.caption_translation_note, post.timeline_context)


# Related-post counts are cached per project (and per public/admin view). The cache lives in this process and is cleared by any
# write request (see the middleware in main.py); on the read-only Vercel deployment data only changes with a deploy, which
# starts fresh processes anyway. The TTL is a safety net for several workers.
RELATED_COUNTS_TTL_SECONDS = 300
_related_counts_cache: dict[tuple[int, bool], tuple[float, dict[str, int]]] = {}


def invalidate_related_counts() -> None:
    _related_counts_cache.clear()


def _get_visible_project(session: Session, project_ref: str) -> Project:
    clean_project_ref = str(project_ref).strip()
    project = session.get(Project, int(clean_project_ref)) if clean_project_ref.isdigit() else session.exec(
        select(Project).where(Project.slug == clean_project_ref.lower())
    ).first()
    if not project or not project.is_visible:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _hashtag_like_conditions(tags) -> list:
    conditions = []
    for tag in tags:
        pattern = f"%#{tag}%"
        conditions.extend((
            Post.caption.ilike(pattern),
            Post.caption_translation.ilike(pattern),
            Post.caption_translation_note.ilike(pattern),
            Post.timeline_context.ilike(pattern),
        ))
    return conditions


def _order_posts(query, sort: str = "newest"):
    """Sort exact-only dates chronologically and mixed/date-only dates manually."""
    same_date_post = aliased(Post)
    date_has_date_only_post = (
        select(same_date_post.id)
        .where(
            same_date_post.parent_id == None,
            same_date_post.posted_at == Post.posted_at,
            same_date_post.posted_at_utc == None,
        )
        .exists()
    )
    manual_order = case((date_has_date_only_post, Post.sort_order), else_=0)
    exact_order = case((date_has_date_only_post, None), else_=Post.posted_at_utc)
    if sort == "newest":
        return query.order_by(
            desc(Post.posted_at),
            manual_order,
            desc(exact_order),
            desc(Post.id),
        )
    return query.order_by(
        Post.posted_at,
        desc(manual_order),
        exact_order,
        Post.id,
    )


def _order_replies(query):
    fallback_utc = func.strftime("%Y-%m-%dT%H:%M:%fZ", Post.posted_at, "-7 hours")
    return query.order_by(
        func.coalesce(Post.posted_at_utc, fallback_utc),
        Post.id,
    )


def _enrich(p: Post, author: Author | None) -> dict:
    """Return a post dict with author info and parsed media_urls list."""
    obj = p.dict()
    fallback_photo = (author.profile_photo_url or author.ig_pfp_url or author.twitter_pfp_url) if author else None
    temp_photo = p.temp_author_pfp_url if p.temp_author_name else None
    display_photo = temp_photo or fallback_photo
    obj["author_name"] = p.temp_author_name or (author.name if author else None)
    obj["author_photo"] = display_photo
    obj["author_ig_pfp_url"] = temp_photo or (author.ig_pfp_url if author else None)
    obj["author_twitter_pfp_url"] = temp_photo or (author.twitter_pfp_url if author else None)
    obj["author_tiktok_pfp_url"] = temp_photo or (author.tiktok_pfp_url if author else None)
    obj["author_instagram_url"] = None if p.temp_author_name else (author.instagram_url if author else None)
    obj["author_broadcast_channel_name"] = None if p.temp_author_name else (author.broadcast_channel_name if author else None)
    # Parse stored JSON array; fall back to [] on bad data
    try:
        raw = json.loads(p.media_urls_json or "[]")
        # Normalize: old format was list of strings; new format is list of objects
        normalized = []
        for item in raw:
            if isinstance(item, str):
                normalized.append({"url": item, "text": None, "translation": None, "note": None})
            else:
                normalized.append(item)
        obj["media_urls"] = normalized
    except Exception:
        obj["media_urls"] = []
    return obj


def _enrich_text(text: PostText, author: Author | None) -> dict:
    obj = text.dict()
    obj["author_name"] = author.name if author else None
    obj["author_photo"] = (author.profile_photo_url or author.ig_pfp_url or author.twitter_pfp_url) if author else None
    obj["author_ig_pfp_url"] = author.ig_pfp_url if author else None
    obj["author_twitter_pfp_url"] = author.twitter_pfp_url if author else None
    obj["author_tiktok_pfp_url"] = author.tiktok_pfp_url if author else None
    obj["author_instagram_url"] = author.instagram_url if author else None
    return obj


def _hydrate_posts(session: Session, posts: list[Post]) -> list[dict]:
    """Attach comments and public child posts in bulk to avoid per-card queries."""
    post_ids = [post.id for post in posts if post.id is not None]
    comments = session.exec(
        select(PostText).where(PostText.post_id.in_(post_ids))
    ).all() if post_ids else []
    replies = session.exec(_order_replies(
        select(Post)
        .outerjoin(Author)
        .where(
            Post.parent_id.in_(post_ids),
            Post.is_visible == True,
            _has_public_author(),
        )
    )).all() if post_ids else []

    author_ids = {
        item.author_id
        for item in [*posts, *comments, *replies]
        if item.author_id is not None
    }
    authors = session.exec(select(Author).where(Author.id.in_(author_ids))).all() if author_ids else []
    authors_by_id = {author.id: author for author in authors}

    comments_by_post = defaultdict(list)
    for comment in comments:
        comments_by_post[comment.post_id].append(
            _enrich_text(comment, authors_by_id.get(comment.author_id))
        )
    replies_by_post = defaultdict(list)
    for reply in replies:
        replies_by_post[reply.parent_id].append(
            _enrich(reply, authors_by_id.get(reply.author_id))
        )

    hydrated = []
    for post in posts:
        obj = _enrich(post, authors_by_id.get(post.author_id))
        obj["comments"] = comments_by_post[post.id]
        obj["childrenPosts"] = replies_by_post[post.id]
        hydrated.append(obj)
    return hydrated


@router.get("/admin")
def get_admin_posts(
    platform: str | None = None,
    author_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    hide_author_categories: str | None = None,
    sort: str = "newest",
    offset: int = 0,
    limit: int = 100,
    session: Session = Depends(get_session),
    _: bool = Depends(require_admin),
):
    query = select(Post).where(Post.parent_id == None)

    query = _filter_post_platform(query, platform)
    query = _filter_admin_author(query, author_id)
    query = _hide_author_categories(query, hide_author_categories)
    if date_from:
        query = query.where(Post.posted_at >= date_from.strip())
    if date_to:
        query = query.where(Post.posted_at <= date_to.strip())

    query = _order_posts(query, sort)

    posts = session.exec(query.offset(offset).limit(limit)).all()
    author_ids = {post.author_id for post in posts if post.author_id is not None}
    authors = session.exec(select(Author).where(Author.id.in_(author_ids))).all() if author_ids else []
    authors_by_id = {author.id: author for author in authors}
    return [_enrich(post, authors_by_id.get(post.author_id)) for post in posts]


@router.get("/admin/count")
def count_admin_posts(
    platform: str | None = None,
    author_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    hide_author_categories: str | None = None,
    session: Session = Depends(get_session),
    _: bool = Depends(require_admin),
):
    query = select(func.count(Post.id)).where(Post.parent_id == None)
    query = _filter_post_platform(query, platform)
    query = _filter_admin_author(query, author_id)
    query = _hide_author_categories(query, hide_author_categories)
    if date_from:
        query = query.where(Post.posted_at >= date_from.strip())
    if date_to:
        query = query.where(Post.posted_at <= date_to.strip())
    return {"count": session.exec(query).one()}


@router.get("/admin/search/count")
def count_admin_post_search(
    q: str,
    platform: str | None = None,
    author_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    hide_author_categories: str | None = None,
    include_text: bool = True,
    include_translations: bool = True,
    include_notes: bool = True,
    include_urls: bool = True,
    include_replies: bool = True,
    session: Session = Depends(get_session),
    _: bool = Depends(require_admin),
):
    term = q.strip()
    if not term:
        return {"count": 0}
    pattern = f"%{term}%"

    post_conditions = []
    if include_text:
        post_conditions.extend((Post.caption.ilike(pattern), Post.temp_author_name.ilike(pattern)))
    if include_translations:
        post_conditions.append(Post.caption_translation.ilike(pattern))
    if include_notes:
        post_conditions.extend((Post.caption_translation_note.ilike(pattern), Post.timeline_context.ilike(pattern)))
    if include_urls:
        post_conditions.extend((Post.external_url.ilike(pattern), Post.media_urls_json.ilike(pattern)))

    total = 0
    if post_conditions:
        post_query = select(func.count(Post.id)).where(or_(*post_conditions))
        if not include_replies:
            post_query = post_query.where(Post.parent_id == None)
        post_query = _filter_post_platform(post_query, platform)
        post_query = _filter_admin_author(post_query, author_id)
        post_query = _hide_author_categories(post_query, hide_author_categories)
        if date_from:
            post_query = post_query.where(Post.posted_at >= date_from.strip())
        if date_to:
            post_query = post_query.where(Post.posted_at <= date_to.strip())
        total += session.exec(post_query).one()

    text_conditions = []
    if include_text:
        text_conditions.append(PostText.content.ilike(pattern))
    if include_translations:
        text_conditions.append(PostText.translation.ilike(pattern))
    if include_notes:
        text_conditions.append(PostText.note.ilike(pattern))
    if include_replies and text_conditions:
        text_query = select(func.count(PostText.id)).where(or_(*text_conditions))
        if (platform and platform != "all") or author_id is not None or hide_author_categories or date_from or date_to:
            text_query = text_query.join(Post)
        if platform and platform != "all":
            text_query = _filter_post_platform(text_query, platform)
        if author_id is not None:
            text_query = _filter_admin_author(text_query, author_id)
        text_query = _hide_author_categories(text_query, hide_author_categories)
        if date_from:
            text_query = text_query.where(func.coalesce(PostText.posted_at, Post.posted_at) >= date_from.strip())
        if date_to:
            text_query = text_query.where(func.coalesce(PostText.posted_at, Post.posted_at) <= date_to.strip())
        total += session.exec(text_query).one()

    return {"count": total}


@router.get("/admin/search")
def search_admin_posts(
    q: str,
    sort: str = "newest",
    platform: str | None = None,
    author_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    hide_author_categories: str | None = None,
    include_text: bool = True,
    include_translations: bool = True,
    include_notes: bool = True,
    include_urls: bool = True,
    include_replies: bool = True,
    offset: int = 0,
    limit: int = 50,
    session: Session = Depends(get_session),
    _: bool = Depends(require_admin),
):
    term = q.strip()
    if not term:
        return []

    pattern = f"%{term}%"

    post_conditions = []
    if include_text:
        post_conditions.append(Post.caption.ilike(pattern))
        post_conditions.append(Post.temp_author_name.ilike(pattern))
    if include_translations:
        post_conditions.append(Post.caption_translation.ilike(pattern))
    if include_notes:
        post_conditions.append(Post.caption_translation_note.ilike(pattern))
        post_conditions.append(Post.timeline_context.ilike(pattern))
    if include_urls:
        post_conditions.extend((Post.external_url.ilike(pattern), Post.media_urls_json.ilike(pattern)))
    if not post_conditions:
        return []

    post_query = select(Post).where(or_(*post_conditions))
    if not include_replies:
        post_query = post_query.where(Post.parent_id == None)

    text_conditions = []
    if include_text:
        text_conditions.append(PostText.content.ilike(pattern))
    if include_translations:
        text_conditions.append(PostText.translation.ilike(pattern))
    if include_notes:
        text_conditions.append(PostText.note.ilike(pattern))
    text_query = select(PostText).where(or_(*text_conditions)) if include_replies and text_conditions else None

    if text_query is not None and ((platform and platform != "all") or author_id is not None or hide_author_categories or date_from or date_to):
        text_query = text_query.join(Post)

    if platform and platform != "all":
        post_query = _filter_post_platform(post_query, platform)
        if text_query is not None:
            text_query = _filter_post_platform(text_query, platform)

    if author_id is not None:
        post_query = _filter_admin_author(post_query, author_id)
        if text_query is not None:
            text_query = _filter_admin_author(text_query, author_id)

    post_query = _hide_author_categories(post_query, hide_author_categories)
    if text_query is not None:
        text_query = _hide_author_categories(text_query, hide_author_categories)

    if date_from:
        start = date_from.strip()
        post_query = post_query.where(Post.posted_at >= start)
        if text_query is not None:
            text_query = text_query.where(func.coalesce(PostText.posted_at, Post.posted_at) >= start)
    if date_to:
        end = date_to.strip()
        post_query = post_query.where(Post.posted_at <= end)
        if text_query is not None:
            text_query = text_query.where(func.coalesce(PostText.posted_at, Post.posted_at) <= end)

    post_matches = session.exec(post_query).all()
    text_matches = session.exec(text_query).all() if text_query is not None else []

    text_post_ids = {text.post_id for text in text_matches}
    text_posts = session.exec(select(Post).where(Post.id.in_(text_post_ids))).all() if text_post_ids else []
    text_posts_by_id = {post.id: post for post in text_posts}
    author_ids = {
        author_id
        for author_id in [
            *(post.author_id for post in post_matches),
            *(post.author_id for post in text_posts),
            *(text.author_id for text in text_matches),
        ]
        if author_id is not None
    }
    authors = session.exec(select(Author).where(Author.id.in_(author_ids))).all() if author_ids else []
    authors_by_id = {author.id: author for author in authors}

    results = []

    for post in post_matches:
        author = authors_by_id.get(post.author_id)
        obj = _enrich(post, author)
        obj["result_id"] = f"post-{post.id}"
        obj["result_type"] = "post" if post.parent_id is None else "x-reply"
        obj["target_post_id"] = post.id if post.parent_id is None else post.parent_id
        selected_match_fields = []
        if include_text:
            selected_match_fields.append(post.caption)
            selected_match_fields.append(post.temp_author_name)
        if include_translations:
            selected_match_fields.append(post.caption_translation)
        if include_notes:
            selected_match_fields.append(post.caption_translation_note)
            selected_match_fields.append(post.timeline_context)
        if include_urls:
            selected_match_fields.append(post.external_url)
        obj["match_text"] = next((value for value in selected_match_fields if value and term.lower() in value.lower()), None)
        if not obj["match_text"] and post.content_type == "broadcast":
            messages = obj.get("media_urls", [])
            obj["match_text"] = next((message.get("text") or message.get("translation") for message in messages if isinstance(message, dict)), None)
        results.append(obj)

    for text in text_matches:
        post = text_posts_by_id.get(text.post_id)
        if not post:
            continue
        author = authors_by_id.get(text.author_id)
        post_author = authors_by_id.get(post.author_id)
        selected_text_fields = []
        if include_text:
            selected_text_fields.append(text.content)
        if include_translations:
            selected_text_fields.append(text.translation)
        if include_notes:
            selected_text_fields.append(text.note)
        match_text = next((value for value in selected_text_fields if value and term.lower() in value.lower()), None)
        results.append({
            "id": text.id,
            "result_id": f"text-{text.id}",
            "result_type": text.type,
            "target_post_id": post.id,
            "post_platform": post.platform,
            "post_content_type": post.content_type,
            "post_author_name": post_author.name if post_author else None,
            "author_id": text.author_id,
            "author_name": author.name if author else None,
            "posted_at": text.posted_at or post.posted_at,
            "is_visible": post.is_visible,
            "external_url": post.external_url,
            "match_text": match_text,
        })

    results.sort(
        key=lambda item: (item.get("posted_at") or "", item.get("result_id") or ""),
        reverse=sort == "newest",
    )
    return results[offset:offset + limit]


@router.post("/admin/{post_id}/order", dependencies=[Depends(require_admin)])
def reorder_post(
    post_id: int,
    payload: PostReorder,
    session: Session = Depends(get_session),
):
    if payload.position not in {"before", "after"}:
        raise HTTPException(status_code=400, detail="Position must be before or after")

    moved = session.get(Post, post_id)
    target = session.get(Post, payload.target_post_id)
    if not moved or not target or moved.parent_id is not None or target.parent_id is not None:
        raise HTTPException(status_code=404, detail="Post not found")
    if (moved.posted_at or "") != (target.posted_at or ""):
        raise HTTPException(status_code=400, detail="Posts can only be reordered within the same date")
    if moved.id == target.id:
        return {"status": "unchanged"}

    posts = session.exec(
        select(Post)
        .where(Post.parent_id == None, Post.posted_at == moved.posted_at)
        .order_by(Post.sort_order, desc(Post.id))
    ).all()

    posts.remove(moved)
    target_index = posts.index(target)
    posts.insert(target_index + (1 if payload.position == "after" else 0), moved)
    for index, post in enumerate(posts):
        post.sort_order = index
        session.add(post)
    session.commit()
    return {"status": "reordered", "post_id": post_id, "sort_order": moved.sort_order}


@router.get("/admin/{post_id}")
def get_admin_post(
    post_id: int,
    session: Session = Depends(get_session),
    _: bool = Depends(require_admin),
):
    post = session.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    author = session.get(Author, post.author_id) if post.author_id else None
    return {"post": _enrich(post, author)}


@router.get("/admin/{post_id}/thread")
def get_admin_thread(
    post_id: int,
    session: Session = Depends(get_session),
    _: bool = Depends(require_admin),
):
    replies = session.exec(_order_replies(
        select(Post).where(Post.parent_id == post_id)
    )).all()
    return [
        _enrich(reply, session.get(Author, reply.author_id) if reply.author_id else None)
        for reply in replies
    ]


def _stored_media_urls(post: Post) -> list[str]:
    urls = []
    if post.media_url:
        urls.append(post.media_url)
    try:
        raw = json.loads(post.media_urls_json or "[]")
        for item in raw:
            url = item if isinstance(item, str) else item.get("url") if isinstance(item, dict) else None
            if isinstance(url, str) and url:
                urls.append(url)
    except (TypeError, ValueError):
        pass
    return list(dict.fromkeys(urls))


def _already_archived(post: Post) -> bool:
    urls = _stored_media_urls(post)
    if not urls:
        return False
    for url in urls:
        try:
            media_router._resolve_public_object(url)
        except HTTPException:
            return False
    return True


@router.post("/admin/{post_id}/archive", dependencies=[Depends(require_admin)])
def archive_instagram_post(
    post_id: int,
    payload: PostArchiveRequest,
    session: Session = Depends(get_session),
):
    """Persist an Instagram post's caption and media into the app's R2 storage."""
    post = session.get(Post, post_id)
    if not post or post.parent_id is not None:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.platform not in {"ig", "instagram"} or post.content_type != "post":
        raise HTTPException(status_code=422, detail="Only Instagram posts and reels can be archived")
    if not post.external_url:
        raise HTTPException(status_code=422, detail="The post has no Instagram source URL")
    if not post.posted_at:
        raise HTTPException(status_code=422, detail="Set the post date before archiving")
    if _already_archived(post):
        raise HTTPException(status_code=409, detail="This post's media is already archived in R2")

    author = session.get(Author, post.author_id) if post.author_id else None
    author_name = post.temp_author_name or (author.name if author else None)
    if not author_name:
        raise HTTPException(status_code=422, detail="Set the post author before archiving")

    shortcode = instagram_shortcode(post.external_url)
    max_bytes = int(os.getenv("R2_MAX_UPLOAD_BYTES", str(media_router.DEFAULT_MAX_UPLOAD_BYTES)))
    uploaded: list[dict[str, str | int]] = []
    try:
        with InstagramPostBrowser(archive_cookie()) as browser:
            captured = browser.capture(post.external_url)
            for index, source in enumerate(captured.media, start=1):
                body, content_type = browser.download(source, max_bytes)
                extension = extension_for_media(content_type)
                filename = f"{author_name}-{post.posted_at}-ig-{shortcode}-{index:02d}{extension}"
                uploaded.append(
                    media_router.store_media_bytes(
                        body,
                        content_type=content_type,
                        destination=payload.destination,
                        author=author_name,
                        posted_at=post.posted_at,
                        media_type="ig",
                        sequence=index,
                        filename=filename,
                    )
                )

        urls = [str(item["url"]) for item in uploaded]
        post.caption = captured.caption if captured.caption is not None else post.caption
        post.media_url = urls[0]
        post.media_urls_json = json.dumps(
            [
                {
                    "url": url,
                    "text": None,
                    "translation": None,
                    "note": None,
                    "attachment_type": None,
                }
                for url in urls
            ]
        )
        post.display_source = "r2"
        session.add(post)
        session.commit()
        session.refresh(post)
    except InstagramArchiveRateLimitError as exc:
        for item in uploaded:
            try:
                media_router.delete_media_key(str(item["bucket"]), str(item["key"]))
            except Exception:
                pass
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except InstagramArchiveSessionError as exc:
        for item in uploaded:
            try:
                media_router.delete_media_key(str(item["bucket"]), str(item["key"]))
            except Exception:
                pass
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except InstagramArchiveError as exc:
        for item in uploaded:
            try:
                media_router.delete_media_key(str(item["bucket"]), str(item["key"]))
            except Exception:
                pass
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception:
        for item in uploaded:
            try:
                media_router.delete_media_key(str(item["bucket"]), str(item["key"]))
            except Exception:
                pass
        raise

    return {
        "archived": True,
        "caption": post.caption,
        "media_urls": urls,
        "post": _enrich(post, author),
    }


@router.get("/timeline")
def get_timeline(
    platform: str | None = None,
    sort: str = "newest",
    offset: int = 0,
    limit: int = 10,
    session: Session = Depends(get_session),
    response: Response = None,
):
    """Return one fully-hydrated timeline page without per-post API calls."""
    if response is not None:
        response.headers["Cache-Control"] = (
            "public, max-age=0, s-maxage=60, stale-while-revalidate=300"
        )

    query = (
        select(Post)
        .outerjoin(Author)
        .where(
            Post.parent_id == None,
            Post.is_visible == True,
            _has_public_author(),
        )
    )
    query = _filter_post_platform(query, platform)

    query = _order_posts(query, sort)

    # Fetch one extra row so the client knows whether a next page exists.
    page_rows = session.exec(query.offset(offset).limit(limit + 1)).all()
    has_more = len(page_rows) > limit
    posts = page_rows[:limit]
    items = _hydrate_posts(session, posts)

    newest_query = (
        select(Post)
        .outerjoin(Author)
        .where(
            Post.parent_id == None,
            Post.is_visible == True,
            _has_public_author(),
        )
    )
    newest = session.exec(_order_posts(newest_query, "newest").limit(1)).first()

    return {
        "items": items,
        "has_more": has_more,
        "last_updated": newest.posted_at if newest else None,
    }


@router.get("/event/{event_id}")
def get_event_post_candidates(
    event_id: int,
    session: Session = Depends(get_session),
    include_hidden: bool = False,
    authorization: str | None = Header(default=None),
):
    """Return public posts that mention one of an event's tags.

    The client applies the shared event/date disambiguation logic so this list
    matches the event links shown on timeline posts. A post also matches when its
    "Related Event / Project" text contains the event's keyword.
    """
    event = session.get(Event, event_id)
    if not event or not event.is_visible:
        raise HTTPException(status_code=404, detail="Event not found")

    try:
        raw_tags = json.loads(event.tags_json or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        raw_tags = []
    tags = {
        str(tag).strip().lstrip("#")
        for tag in raw_tags
        if str(tag).strip().lstrip("#")
    }
    keyword = (event.keyword or "").strip()
    if not tags and not keyword:
        return []

    conditions = []
    for tag in tags:
        pattern = f"%#{tag}%"
        conditions.extend((
            Post.caption.ilike(pattern),
            Post.caption_translation.ilike(pattern),
            Post.caption_translation_note.ilike(pattern),
            Post.timeline_context.ilike(pattern),
        ))
    if keyword:
        # An event keyword typed into "Related Event / Project" links the post to the event.
        conditions.append(Post.timeline_context.ilike(f"%{keyword}%"))

    query = (
        select(Post)
        .outerjoin(Author)
        .where(
            Post.parent_id == None,
            _related_page_filter(include_hidden, authorization),
            _has_public_author(),
            or_(*conditions),
        )
    )
    posts = session.exec(_order_posts(query, "newest")).all()
    return _hydrate_posts(session, posts)


@router.get("/project/{project_ref}/related")
def get_project_post_candidates(
    project_ref: str,
    hashtag: str = "",
    session: Session = Depends(get_session),
    include_hidden: bool = False,
    authorization: str | None = Header(default=None),
    entry_type: str | None = None,
    entry_number: int | None = None,
):
    """Return posts related to a project, filming day, episode, fitting or workshop.

    A post is related when its text has the row's `hashtag`, or when it was explicitly linked to the row
    (`entry_type` + `entry_number`) in the post form, which also works for rows without a hashtag.
    """
    project = _get_visible_project(session, project_ref)
    rows = _project_entry_rows(session, project.id)

    allowed_tags = {
        str(value).strip().lstrip("#").casefold()
        for value in [project.hashtag, *[tag for _type, _number, tag in rows]]
        if str(value or "").strip().lstrip("#")
    }
    clean_hashtag = hashtag.strip().lstrip("#")
    if clean_hashtag and clean_hashtag.casefold() not in allowed_tags:
        raise HTTPException(status_code=404, detail="Project hashtag not found")

    link = None
    if entry_type is not None or entry_number is not None:
        if (entry_type, entry_number) not in {(row_type, row_number) for row_type, row_number, _tag in rows}:
            raise HTTPException(status_code=404, detail="Project entry not found")
        link = (project.id, entry_type, entry_number)
    if not clean_hashtag and link is None:
        return []

    conditions = _hashtag_like_conditions([clean_hashtag] if clean_hashtag else [])
    if link is not None:
        conditions.append(Post.project_entry_links_json.like(_project_entry_link_like(*link)))
    query = (
        select(Post)
        .outerjoin(Author)
        .where(
            Post.parent_id == None,
            _related_page_filter(include_hidden, authorization),
            _has_public_author(),
            or_(*conditions),
        )
    )
    normalized_hashtag = clean_hashtag.casefold()
    posts = [
        post for post in session.exec(_order_posts(query, "newest")).all()
        if (normalized_hashtag and normalized_hashtag in _hashtags_in_post(post))
        or (link is not None and link in _post_project_entry_links(post))
    ]
    return _hydrate_posts(session, posts)


def _compute_related_counts(session: Session, project: Project, include_hidden: bool, authorization: str | None) -> dict[str, int]:
    """One pass over the candidate posts, whatever the number of rows (no per-row rescans).

    Posts are fetched with one query that selects only the columns needed, a post's hashtags are extracted once, and each
    hashtag / explicit link is looked up in a dict to find the row(s) it counts for.
    """
    rows = _project_entry_rows(session, project.id)
    rows_by_tag: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for entry_type, number, value in rows:
        tag = str(value or "").strip().lstrip("#").casefold()
        if tag:
            rows_by_tag[tag].append((entry_type, number))

    # Cheap candidate filter: a post can only count when it has some hashtag or links to this project at all.
    link_like = _project_entry_link_like(project.id)
    query = (
        select(
            Post.id, Post.caption, Post.caption_translation, Post.caption_translation_note,
            Post.timeline_context, Post.project_entry_links_json,
        )
        .outerjoin(Author)
        .where(
            Post.parent_id == None,
            _related_page_filter(include_hidden, authorization),
            _has_public_author(),
            or_(
                Post.caption.like("%#%"),
                Post.caption_translation.like("%#%"),
                Post.caption_translation_note.like("%#%"),
                Post.timeline_context.like("%#%"),
                Post.project_entry_links_json.like(link_like),
            ),
        )
    )

    post_ids_by_row: dict[tuple[str, int], set[int]] = defaultdict(set)
    for post_id, caption, translation, note, context, links_json in session.exec(query).all():
        for tag in _hashtags_in_text(caption, translation, note, context):
            for row in rows_by_tag.get(tag, ()):
                post_ids_by_row[row].add(post_id)
        if links_json and link_like.strip("%") in links_json:
            for project_id, entry_type, number in _links_from_json(links_json):
                if project_id == project.id:
                    post_ids_by_row[(entry_type, number)].add(post_id)

    counts: dict[str, int] = {}
    for entry_type, number, value in rows:
        total = len(post_ids_by_row.get((entry_type, number), ()))
        counts[f"{entry_type}:{number}"] = total
        tag = str(value or "").strip().lstrip("#")
        if tag:
            counts[tag] = total
    return counts


@router.get("/project/{project_ref}/related-counts")
def get_project_related_post_counts(
    project_ref: str,
    session: Session = Depends(get_session),
    include_hidden: bool = False,
    authorization: str | None = Header(default=None),
    response: Response = None,
):
    """Related-post counts per project row.

    Keys: every row hashtag, plus `<entry_type>:<number>` for every row (`filming:3`, `fitting:1`, …) so rows
    without a hashtag are counted too. A post counts once per row, by hashtag or by explicit link.

    The public answer is cacheable by a CDN (`s-maxage`); the admin answer (`include_hidden`) is never shared.
    """
    project = _get_visible_project(session, project_ref)
    _related_page_filter(include_hidden, authorization)  # an `include_hidden` request needs a valid admin token, cached or not

    if response is not None:
        response.headers["Cache-Control"] = (
            "private, no-store" if include_hidden
            else f"public, max-age=0, s-maxage={RELATED_COUNTS_TTL_SECONDS}, stale-while-revalidate=60"
        )

    key = (project.id, include_hidden)
    cached = _related_counts_cache.get(key)
    if cached and time.monotonic() - cached[0] < RELATED_COUNTS_TTL_SECONDS:
        return dict(cached[1])

    counts = _compute_related_counts(session, project, include_hidden, authorization)
    _related_counts_cache[key] = (time.monotonic(), counts)
    return dict(counts)


@router.get("/{post_id}")
def get_post(post_id: int, session: Session = Depends(get_session)):
    post = session.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    author = session.get(Author, post.author_id) if post.author_id else None
    has_temp_author = bool((post.temp_author_name or "").strip())
    if not post.is_visible or not (has_temp_author or (author and author.show_on_timeline)):
        raise HTTPException(status_code=404, detail="Post not found")

    return {"post": _enrich(post, author)}

# -----------------------------
# CREATE MAIN POST (IG or X)
# -----------------------------


@router.post("/", dependencies=[Depends(require_admin)])
def create_post(post: Post, session: Session = Depends(get_session)):
    post.project_entry_links_json = _normalize_project_entry_links(post.project_entry_links_json)
    _normalize_post_author(post)
    _normalize_display_source(post)
    post.posted_at_utc = _normalize_utc_timestamp(post.posted_at_utc)
    if post.parent_id is not None:
        parent = session.get(Post, post.parent_id)
        if not parent:
            raise HTTPException(status_code=404, detail="Parent post not found")
        _validate_reply_timing(post.posted_at, post.posted_at_utc, parent)
    if post.parent_id is None:
        current_first = session.exec(
            select(Post)
            .where(Post.parent_id == None, Post.posted_at == post.posted_at)
            .order_by(Post.sort_order)
            .limit(1)
        ).first()
        post.sort_order = (current_first.sort_order - 1) if current_first else 0
    session.add(post)
    session.commit()
    session.refresh(post)
    return post


# -----------------------------
# GET ONE POST (with children + comments loaded)
# -----------------------------
@router.get("/")
def get_posts(
    platform: str | None = None,
    sort: str = "newest",
    offset: int = 0,
    limit: int = 10,
    session: Session = Depends(get_session)
):
    query = (
        select(Post)
        .outerjoin(Author)
        .where(
            Post.parent_id == None,
            Post.is_visible == True,
            _has_public_author(),
        )
    )

    query = _filter_post_platform(query, platform)

    query = _order_posts(query, sort)

    # Apply pagination
    query = query.offset(offset).limit(limit)

    posts = session.exec(query).all()

    enriched = []
    for p in posts:
        author = session.get(Author, p.author_id) if p.author_id else None
        enriched.append(_enrich(p, author))

    return enriched

# -----------------------------
# CREATE A TWEET REPLY (child Post)
# -----------------------------
# @router.post("/{post_id}/reply", dependencies=[Depends(require_admin)])
# def create_reply(
#     post_id: int,
#     reply: Post,
#     session: Session = Depends(get_session)
# ):
#     parent = session.get(Post, post_id)
#     if not parent:
#         raise HTTPException(status_code=404, detail="Parent post not found")

#     reply.parent_id = post_id
#     session.add(reply)
#     session.commit()
#     session.refresh(reply)
#     return reply


@router.post("/{post_id}/reply", dependencies=[Depends(require_admin)])
def create_reply(post_id: int, reply: Post, session: Session = Depends(get_session)):
    parent = session.get(Post, post_id)
    if not parent:
        raise HTTPException(status_code=404, detail="Parent post not found")

    # Only X uses Post-children threading
    if parent.platform != "x":
        raise HTTPException(
            status_code=400, detail="Only X posts support /reply threads")

    reply.parent_id = post_id
    reply.platform = "x"  # enforce
    _normalize_post_author(reply)
    reply.posted_at_utc = _normalize_utc_timestamp(reply.posted_at_utc)
    _validate_reply_timing(reply.posted_at, reply.posted_at_utc, parent)
    session.add(reply)
    session.commit()
    session.refresh(reply)
    return reply

# -----------------------------
# GET TWEET THREAD
# -----------------------------


@router.get("/{post_id}/thread")
def get_thread(post_id: int, session: Session = Depends(get_session)):
    replies = session.exec(
        select(Post)
        .outerjoin(Author)
        .where(
            Post.parent_id == post_id,
            Post.is_visible == True,
            _has_public_author(),
        )
    ).all()

    enriched = []
    for r in replies:
        author = session.get(Author, r.author_id) if r.author_id else None
        enriched.append(_enrich(r, author))

    return enriched


# -----------------------------
# DELETE POST (full cascade)
# -----------------------------
@router.delete("/{post_id}", dependencies=[Depends(require_admin)])
def delete_post(post_id: int, session: Session = Depends(get_session)):
    post = session.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Not found")

    # delete children tweet replies
    children = session.exec(
        select(Post).where(Post.parent_id == post_id)
    ).all()
    for child in children:
        session.delete(child)

    # delete IG comments
    comments = session.exec(
        select(PostText).where(PostText.post_id == post_id)
    ).all()
    for c in comments:
        session.delete(c)

    # delete main post
    session.delete(post)
    session.commit()
    return {"status": "deleted"}

# -----------------------------
# UPDATE POST (EDIT)
# -----------------------------


@router.patch("/{post_id}", dependencies=[Depends(require_admin)])
def update_post(post_id: int, updates: dict, session: Session = Depends(get_session)):

    post = session.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    if "posted_at_utc" in updates:
        updates["posted_at_utc"] = _normalize_utc_timestamp(updates["posted_at_utc"])
    if "project_entry_links_json" in updates:
        updates["project_entry_links_json"] = _normalize_project_entry_links(updates["project_entry_links_json"])

    if post.parent_id is not None:
        parent = session.get(Post, post.parent_id)
        _validate_reply_timing(
            updates.get("posted_at", post.posted_at),
            updates.get("posted_at_utc", post.posted_at_utc),
            parent,
        )

    next_posted_at = updates.get("posted_at")
    if post.parent_id is None and next_posted_at is not None and next_posted_at != post.posted_at:
        current_first = session.exec(
            select(Post)
            .where(Post.parent_id == None, Post.posted_at == next_posted_at, Post.id != post.id)
            .order_by(Post.sort_order)
            .limit(1)
        ).first()
        updates["sort_order"] = (current_first.sort_order - 1) if current_first else 0

    # Apply updates dynamically
    for key, value in updates.items():
        if hasattr(post, key):
            setattr(post, key, value)

    _normalize_post_author(post)
    _normalize_display_source(post)

    session.add(post)
    session.commit()
    session.refresh(post)

    return post
