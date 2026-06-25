import json
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlmodel import Session, select, desc
from database import get_session
from models import Post, PostText, Author
from middleware.auth import require_admin

router = APIRouter(prefix="/posts", tags=["Posts"])


def _enrich(p: Post, author: Author | None) -> dict:
    """Return a post dict with author info and parsed media_urls list."""
    obj = p.dict()
    obj["author_name"] = author.name if author else None
    obj["author_photo"] = (author.profile_photo_url or author.ig_pfp_url or author.twitter_pfp_url) if author else None
    obj["author_ig_pfp_url"] = author.ig_pfp_url if author else None
    obj["author_twitter_pfp_url"] = author.twitter_pfp_url if author else None
    obj["author_tiktok_pfp_url"] = author.tiktok_pfp_url if author else None
    obj["author_instagram_url"] = author.instagram_url if author else None
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


@router.get("/admin")
def get_admin_posts(
    platform: str | None = None,
    sort: str = "newest",
    offset: int = 0,
    limit: int = 100,
    session: Session = Depends(get_session),
    _: bool = Depends(require_admin),
):
    query = select(Post).where(Post.parent_id == None)

    if platform:
        query = query.where(Post.platform == platform)

    if sort == "newest":
        query = query.order_by(desc(Post.posted_at), desc(Post.id))
    else:
        query = query.order_by(Post.posted_at, Post.id)

    posts = session.exec(query.offset(offset).limit(limit)).all()

    enriched = []
    for p in posts:
        author = session.get(Author, p.author_id) if p.author_id else None
        enriched.append(_enrich(p, author))

    return enriched


@router.get("/admin/search")
def search_admin_posts(
    q: str,
    platform: str | None = None,
    offset: int = 0,
    limit: int = 50,
    session: Session = Depends(get_session),
    _: bool = Depends(require_admin),
):
    term = q.strip()
    if not term:
        return []

    pattern = f"%{term}%"

    post_query = select(Post).where(
        or_(
            Post.caption.ilike(pattern),
            Post.caption_translation.ilike(pattern),
            Post.caption_translation_note.ilike(pattern),
            Post.external_url.ilike(pattern),
        )
    )

    text_query = select(PostText).where(
        or_(
            PostText.content.ilike(pattern),
            PostText.translation.ilike(pattern),
            PostText.note.ilike(pattern),
        )
    )

    if platform and platform != "all":
        post_query = post_query.where(Post.platform == platform)
        text_query = text_query.join(Post).where(Post.platform == platform)

    post_matches = session.exec(post_query).all()
    text_matches = session.exec(text_query).all()

    results = []

    for post in post_matches:
        author = session.get(Author, post.author_id) if post.author_id else None
        obj = _enrich(post, author)
        obj["result_id"] = f"post-{post.id}"
        obj["result_type"] = "post" if post.parent_id is None else "x-reply"
        obj["target_post_id"] = post.id if post.parent_id is None else post.parent_id
        obj["match_text"] = post.caption or post.caption_translation or post.caption_translation_note or post.external_url
        results.append(obj)

    for text in text_matches:
        post = session.get(Post, text.post_id)
        if not post:
            continue
        author = session.get(Author, text.author_id) if text.author_id else None
        post_author = session.get(Author, post.author_id) if post.author_id else None
        results.append({
            "id": text.id,
            "result_id": f"text-{text.id}",
            "result_type": text.type,
            "target_post_id": post.id,
            "post_platform": post.platform,
            "post_author_name": post_author.name if post_author else None,
            "author_id": text.author_id,
            "author_name": author.name if author else None,
            "posted_at": text.posted_at or post.posted_at,
            "is_visible": post.is_visible,
            "external_url": post.external_url,
            "match_text": text.content or text.translation or text.note,
        })

    results.sort(key=lambda item: (item.get("posted_at") or "", item.get("result_id") or ""), reverse=True)
    return results[offset:offset + limit]


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

    if platform:
        query = query.where(Post.platform == platform)

    # Sorting
    if sort == "newest":
        query = query.order_by(desc(Post.posted_at), desc(Post.id))
    else:
        query = query.order_by(Post.posted_at, Post.id)

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

    # Apply updates dynamically
    for key, value in updates.items():
        if hasattr(post, key):
            setattr(post, key, value)

    session.add(post)
    session.commit()
    session.refresh(post)

    return post
