from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select, desc
from database import get_session
from models import Post, PostText, Author
from middleware.auth import require_admin

router = APIRouter(prefix="/posts", tags=["Posts"])


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
# LIST POSTS (with sort + filter)
# -----------------------------
@router.get("/")
def get_posts(
    platform: str | None = None,
    sort: str | None = None,
    session: Session = Depends(get_session)
):
    query = select(Post).where(Post.parent_id == None)

    if platform:
        query = query.where(Post.platform == platform)

    if sort == "newest":
        query = query.order_by(desc(Post.posted_at))
    elif sort == "oldest":
        query = query.order_by(Post.posted_at)

    posts = session.exec(query).all()

    enriched = []
    for p in posts:
        author = session.get(Author, p.author_id) if p.author_id else None

        post_data = p.dict()
        post_data["author_name"] = author.name if author else None
        post_data["author_photo"] = author.profile_photo_url if author else None

        enriched.append(post_data)

    return enriched


# -----------------------------
# GET ONE POST (with children + comments loaded)
# -----------------------------
@router.get("/{post_id}")
def get_one_post(post_id: int, session: Session = Depends(get_session)):
    post = session.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Not found")

    # Load children
    children = session.exec(
        select(Post).where(Post.parent_id == post_id)
    ).all()

    # Load IG replies
    comments = session.exec(
        select(PostText).where(PostText.post_id == post_id)
    ).all()

    # Expand post with author fields
    post_data = post.dict()
    post_data["author_name"] = post.author_name
    post_data["author_photo"] = post.author_photo

    # Expand children with author fields
    children_data = []
    for c in children:
        cd = c.dict()
        cd["author_name"] = c.author_name
        cd["author_photo"] = c.author_photo
        children_data.append(cd)

    # Expand comments with author fields
    comments_data = []
    for c in comments:
        cd = c.dict()
        cd["author_name"] = c.author_name
        cd["author_photo"] = c.author_photo
        comments_data.append(cd)

    return {
        "post": post_data,
        "children": children_data,
        "comments": comments_data
    }


# -----------------------------
# CREATE A TWEET REPLY (child Post)
# -----------------------------
@router.post("/{post_id}/reply", dependencies=[Depends(require_admin)])
def create_reply(
    post_id: int,
    reply: Post,
    session: Session = Depends(get_session)
):
    parent = session.get(Post, post_id)
    if not parent:
        raise HTTPException(status_code=404, detail="Parent post not found")

    reply.parent_id = post_id
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
        select(Post).where(Post.parent_id == post_id)
    ).all()

    enriched = []
    for r in replies:
        author = session.get(Author, r.author_id) if r.author_id else None

        rd = r.dict()
        rd["author_name"] = author.name if author else None
        rd["author_photo"] = author.profile_photo_url if author else None

        enriched.append(rd)

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
