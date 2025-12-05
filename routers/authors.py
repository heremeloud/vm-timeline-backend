from fastapi import APIRouter, HTTPException, Depends
from sqlmodel import Session, select
from database import get_session
from models import Author
from middleware.auth import require_admin

router = APIRouter(prefix="/authors", tags=["Authors"])


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
@router.post("/", response_model=Author)
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
@router.patch("/{author_id}", response_model=Author)
def update_author(author_id: int, update: Author, session: Session = Depends(get_session)):
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


@router.post("/ensure", response_model=Author)
def ensure_author(author: Author, session: Session = Depends(get_session)):
    existing = session.exec(select(Author).where(
        Author.name == author.name)).first()
    if existing:
        return existing

    session.add(author)
    session.commit()
    session.refresh(author)
    return author
