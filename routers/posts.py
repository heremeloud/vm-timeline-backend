import json
from collections import defaultdict
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_
from sqlmodel import Session, SQLModel, select, desc
from database import get_session
from models import Post, PostText, Author
from middleware.auth import require_admin

router = APIRouter(prefix="/posts", tags=["Posts"])


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


def _order_posts(query, sort: str = "newest"):
    """Use exact time when known; retain manual ordering as the date-only fallback."""
    # A date-only record is treated as Bangkok midnight for its chronological
    # position. Its existing sort_order remains the tie-breaker.
    fallback_utc = func.strftime("%Y-%m-%dT%H:%M:%fZ", Post.posted_at, "-7 hours")
    chronological_time = func.coalesce(Post.posted_at_utc, fallback_utc)
    if sort == "newest":
        return query.order_by(
            desc(chronological_time),
            Post.sort_order,
            desc(Post.id),
        )
    return query.order_by(
        chronological_time,
        desc(Post.sort_order),
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
    obj["author_name"] = author.name if author else None
    obj["author_photo"] = (author.profile_photo_url or author.ig_pfp_url or author.twitter_pfp_url) if author else None
    obj["author_ig_pfp_url"] = author.ig_pfp_url if author else None
    obj["author_twitter_pfp_url"] = author.twitter_pfp_url if author else None
    obj["author_tiktok_pfp_url"] = author.tiktok_pfp_url if author else None
    obj["author_instagram_url"] = author.instagram_url if author else None
    obj["author_broadcast_channel_name"] = author.broadcast_channel_name if author else None
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


@router.get("/admin")
def get_admin_posts(
    platform: str | None = None,
    author_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "newest",
    offset: int = 0,
    limit: int = 100,
    session: Session = Depends(get_session),
    _: bool = Depends(require_admin),
):
    query = select(Post).where(Post.parent_id == None)

    query = _filter_post_platform(query, platform)
    if author_id is not None:
        query = query.where(Post.author_id == author_id)
    if date_from:
        query = query.where(Post.posted_at >= date_from.strip())
    if date_to:
        query = query.where(Post.posted_at <= date_to.strip())

    query = _order_posts(query, sort)

    posts = session.exec(query.offset(offset).limit(limit)).all()

    enriched = []
    for p in posts:
        author = session.get(Author, p.author_id) if p.author_id else None
        enriched.append(_enrich(p, author))

    return enriched


@router.get("/admin/search")
def search_admin_posts(
    q: str,
    sort: str = "newest",
    platform: str | None = None,
    author_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
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

    if text_query is not None and ((platform and platform != "all") or author_id is not None or date_from or date_to):
        text_query = text_query.join(Post)

    if platform and platform != "all":
        post_query = _filter_post_platform(post_query, platform)
        if text_query is not None:
            text_query = _filter_post_platform(text_query, platform)

    if author_id is not None:
        post_query = post_query.where(Post.author_id == author_id)
        if text_query is not None:
            text_query = text_query.where(Post.author_id == author_id)

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

    results = []

    for post in post_matches:
        author = session.get(Author, post.author_id) if post.author_id else None
        obj = _enrich(post, author)
        obj["result_id"] = f"post-{post.id}"
        obj["result_type"] = "post" if post.parent_id is None else "x-reply"
        obj["target_post_id"] = post.id if post.parent_id is None else post.parent_id
        selected_match_fields = []
        if include_text:
            selected_match_fields.append(post.caption)
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
        post = session.get(Post, text.post_id)
        if not post:
            continue
        author = session.get(Author, text.author_id) if text.author_id else None
        post_author = session.get(Author, post.author_id) if post.author_id else None
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


@router.get("/timeline")
def get_timeline(
    platform: str | None = None,
    sort: str = "newest",
    offset: int = 0,
    limit: int = 10,
    session: Session = Depends(get_session),
):
    """Return one fully-hydrated timeline page without per-post API calls."""
    query = (
        select(Post)
        .join(Author)
        .where(
            Post.parent_id == None,
            Post.is_visible == True,
            Author.show_on_timeline == True,
        )
    )
    query = _filter_post_platform(query, platform)

    query = _order_posts(query, sort)

    # Fetch one extra row so the client knows whether a next page exists.
    page_rows = session.exec(query.offset(offset).limit(limit + 1)).all()
    has_more = len(page_rows) > limit
    posts = page_rows[:limit]
    post_ids = [post.id for post in posts if post.id is not None]

    comments = []
    replies = []
    if post_ids:
        comments = session.exec(
            select(PostText).where(PostText.post_id.in_(post_ids))
        ).all()
        reply_query = (
            select(Post)
            .join(Author)
            .where(
                Post.parent_id.in_(post_ids),
                Post.is_visible == True,
                Author.show_on_timeline == True,
            )
        )
        replies = session.exec(_order_replies(reply_query)).all()

    author_ids = {
        item.author_id
        for item in [*posts, *comments, *replies]
        if item.author_id is not None
    }
    authors = (
        session.exec(select(Author).where(Author.id.in_(author_ids))).all()
        if author_ids
        else []
    )
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

    items = []
    for post in posts:
        obj = _enrich(post, authors_by_id.get(post.author_id))
        obj["comments"] = comments_by_post[post.id]
        obj["childrenPosts"] = replies_by_post[post.id]
        items.append(obj)

    newest_query = (
        select(Post)
        .join(Author)
        .where(
            Post.parent_id == None,
            Post.is_visible == True,
            Author.show_on_timeline == True,
        )
    )
    newest = session.exec(_order_posts(newest_query, "newest").limit(1)).first()

    return {
        "items": items,
        "has_more": has_more,
        "last_updated": newest.posted_at if newest else None,
    }


@router.get("/{post_id}")
def get_post(post_id: int, session: Session = Depends(get_session)):
    post = session.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    author = session.get(Author, post.author_id) if post.author_id else None
    if not post.is_visible or not author or not author.show_on_timeline:
        raise HTTPException(status_code=404, detail="Post not found")

    return {"post": _enrich(post, author)}

# -----------------------------
# CREATE MAIN POST (IG or X)
# -----------------------------


@router.post("/", dependencies=[Depends(require_admin)])
def create_post(post: Post, session: Session = Depends(get_session)):
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
        .join(Author)
        .where(
            Post.parent_id == None,
            Post.is_visible == True,
            Author.show_on_timeline == True,
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
        .join(Author)
        .where(
            Post.parent_id == post_id,
            Post.is_visible == True,
            Author.show_on_timeline == True,
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

    session.add(post)
    session.commit()
    session.refresh(post)

    return post
