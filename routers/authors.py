import os
import shutil
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File
from sqlmodel import Session, SQLModel, select
from database import get_session
from models import Author
from middleware.auth import require_admin

UPLOAD_DIR = "uploads/authors"

router = APIRouter(prefix="/authors", tags=["Authors"])


class AuthorUpdate(SQLModel):
    name: Optional[str] = None
    full_name: Optional[str] = None
    profile_photo_url: Optional[str] = None
    ig_pfp_url: Optional[str] = None
    twitter_pfp_url: Optional[str] = None
    tiktok_pfp_url: Optional[str] = None
    birthday: Optional[str] = None
    twitter_url: Optional[str] = None
    instagram_url: Optional[str] = None
    tiktok_url: Optional[str] = None
    gmmtv_url: Optional[str] = None
    mydramalist_url: Optional[str] = None
    fc_url: Optional[str] = None
    show_on_timeline: Optional[bool] = None


# ---------------------------------------------------------
# PUBLIC — Anyone can fetch authors
# ---------------------------------------------------------
@router.get("/", response_model=list[Author])
def get_all_authors(session: Session = Depends(get_session)):
    return session.exec(select(Author)).all()


@router.get("/{author_id}", response_model=Author)
def get_author(author_id: int, session: Session = Depends(get_session)):
    author = session.get(Author, author_id)
    if not author:
        raise HTTPException(status_code=404, detail="Author not found")
    return author


# ---------------------------------------------------------
# ADMIN ONLY — Create author
# ---------------------------------------------------------
@router.post("/", response_model=Author, dependencies=[Depends(require_admin)])
def create_author(author: Author, session: Session = Depends(get_session)):
    # Unique constraint check
    existing = session.exec(select(Author).where(
        Author.name == author.name)).first()
    if existing:
        raise HTTPException(
            status_code=400, detail="Author name already exists")

    session.add(author)
    session.commit()
    session.refresh(author)
    return author


# ---------------------------------------------------------
# ADMIN ONLY — Update author
# ---------------------------------------------------------
@router.patch("/{author_id}", response_model=Author, dependencies=[Depends(require_admin)])
def update_author(author_id: int, update: AuthorUpdate, session: Session = Depends(get_session)):
    author = session.get(Author, author_id)
    if not author:
        raise HTTPException(status_code=404, detail="Author not found")

    update_data = update.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(author, key, value)

    session.add(author)
    session.commit()
    session.refresh(author)
    return author


# ---------------------------------------------------------
# ADMIN ONLY — Delete author
# ---------------------------------------------------------
@router.delete("/{author_id}", dependencies=[Depends(require_admin)])
def delete_author(author_id: int, session: Session = Depends(get_session)):
    author = session.get(Author, author_id)
    if not author:
        raise HTTPException(status_code=404, detail="Author not found")

    session.delete(author)
    session.commit()
    return {"message": "Author deleted successfully"}


@router.post("/{author_id}/upload-photo", dependencies=[Depends(require_admin)])
def upload_author_photo(author_id: int, file: UploadFile = File(...), session: Session = Depends(get_session)):
    author = session.get(Author, author_id)
    if not author:
        raise HTTPException(status_code=404, detail="Author not found")

    ext = os.path.splitext(file.filename or "")[-1].lower() or ".jpg"
    filename = f"{author_id}{ext}"
    filepath = os.path.join(UPLOAD_DIR, filename)

    with open(filepath, "wb") as f:
        shutil.copyfileobj(file.file, f)

    author.profile_photo_url = f"/static/authors/{filename}"
    session.add(author)
    session.commit()
    session.refresh(author)
    return author


@router.post("/ensure", response_model=Author, dependencies=[Depends(require_admin)])
def ensure_author(author: Author, session: Session = Depends(get_session)):
    existing = session.exec(select(Author).where(
        Author.name == author.name)).first()
    if existing:
        return existing

    session.add(author)
    session.commit()
    session.refresh(author)
    return author
