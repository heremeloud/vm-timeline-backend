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
# DELETE COMMENT PAIR (main + translation)
# ---------------------------------
@router.delete("/pair/{text_id}", dependencies=[Depends(require_admin)])
def delete_pair(text_id: int, session: Session = Depends(get_session)):
    parent = session.get(PostText, text_id)
    if not parent:
        raise HTTPException(status_code=404, detail="Comment not found")

    # Delete translation(s)
    translations = session.exec(
        select(PostText).where(PostText.parent_comment_id == text_id)
    ).all()

    for t in translations:
        session.delete(t)

    # Delete main comment
    session.delete(parent)
    session.commit()

    return {"message": "IG reply pair deleted", "id": text_id}


# ---------------------------------
# EDIT IG COMMENT + TRANSLATION
# ---------------------------------
@router.patch("/pair/{text_id}", dependencies=[Depends(require_admin)])
def edit_pair(text_id: int, payload: dict, session: Session = Depends(get_session)):

    parent = session.get(PostText, text_id)
    if not parent:
        raise HTTPException(status_code=404, detail="Comment not found")

    # Update parent comment
    parent.content = payload.get("caption", parent.content)
    parent.media_url = payload.get("media_url", parent.media_url)

    # FIX: use author_id instead of author
    if "author_id" in payload:
        parent.author_id = payload["author_id"]

    # Find translation row
    child = session.exec(
        select(PostText).where(PostText.parent_comment_id == text_id)
    ).first()

    new_trans = payload.get("translation")

    # If translation was removed
    if new_trans is None or new_trans.strip() == "":
        if child:
            session.delete(child)

    else:
        # If translation exists, update it
        if child:
            child.content = new_trans
        else:
            new_child = PostText(
                post_id=parent.post_id,
                type="ig-translation",
                language="en",
                content=new_trans,
                parent_comment_id=text_id,
                author_id=parent.author_id,   # also use author_id here
            )
            session.add(new_child)

    session.commit()
    return {"message": "IG reply updated"}
