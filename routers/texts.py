from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from database import get_session
from models import Post, PostText, Author
from middleware.auth import require_admin

router = APIRouter(prefix="/texts", tags=["Texts"])


# ---------------------------------
# ADD IG COMMENT OR TRANSLATION
# ---------------------------------

@router.post("/", dependencies=[Depends(require_admin)])
def add_text(text: PostText, session: Session = Depends(get_session)):
    post = session.get(Post, text.post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    # Optional: enforce type matches platform
    if text.type.startswith("ig-") and post.platform != "ig":
        raise HTTPException(
            status_code=400, detail="ig-* text must belong to an IG post")
    if text.type.startswith("tt-") and post.platform != "tt":
        raise HTTPException(
            status_code=400, detail="tt-* text must belong to a TikTok post")

    session.add(text)
    session.commit()
    session.refresh(text)
    return text


# ---------------------------------
# GET ALL COMMENTS / TEXT BY POST
# ---------------------------------
@router.get("/by_post/{post_id}")
def get_by_post(post_id: int, session: Session = Depends(get_session)):
    comments = session.exec(
        select(PostText).where(PostText.post_id == post_id)
    ).all()

    enriched = []
    for c in comments:
        author = session.get(Author, c.author_id) if c.author_id else None

        cd = c.dict()
        cd["author_name"] = author.name if author else None
        cd["author_photo"] = author.profile_photo_url if author else None

        enriched.append(cd)

    return enriched


# ---------------------------------
# DELETE COMMENT (and any legacy translation children)
# ---------------------------------
@router.delete("/pair/{text_id}", dependencies=[Depends(require_admin)])
def delete_pair(text_id: int, session: Session = Depends(get_session)):
    parent = session.get(PostText, text_id)
    if not parent:
        raise HTTPException(status_code=404, detail="Comment not found")

    # Delete any legacy child translation rows
    translations = session.exec(
        select(PostText).where(PostText.parent_comment_id == text_id)
    ).all()
    for t in translations:
        session.delete(t)

    session.delete(parent)
    session.commit()

    return {"message": "Reply deleted", "id": text_id}


# ---------------------------------
# EDIT IG/TT COMMENT + TRANSLATION
# ---------------------------------

@router.patch("/pair/{text_id}", dependencies=[Depends(require_admin)])
def edit_pair(text_id: int, payload: dict, session: Session = Depends(get_session)):

    parent = session.get(PostText, text_id)
    if not parent:
        raise HTTPException(status_code=404, detail="Comment not found")

    # Update fields directly on the parent record
    if "caption" in payload:
        parent.content = payload["caption"] or None
    if "translation" in payload:
        t = payload["translation"]
        parent.translation = t.strip() if t and t.strip() else None
    if "note" in payload:
        n = payload["note"]
        parent.note = n.strip() if n and n.strip() else None
    if "media_url" in payload:
        parent.media_url = payload["media_url"] or None
    if "author_id" in payload:
        parent.author_id = payload["author_id"]

    session.commit()
    return {"message": "Reply updated"}
