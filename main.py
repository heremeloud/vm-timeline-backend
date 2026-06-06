
from dotenv import load_dotenv
load_dotenv()  
from middleware import auth
import os
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from database import create_db_and_tables, run_migrations

# Routers
from routers import posts, texts, authors, events, projects, topics


app = FastAPI(title="VM Social Timeline API")


# ---------------------------------------------------------
# Startup Event
# ---------------------------------------------------------
UPLOAD_DIR = "uploads"
os.makedirs(os.path.join(UPLOAD_DIR, "authors"), exist_ok=True)
app.mount("/static", StaticFiles(directory=UPLOAD_DIR), name="static")

@app.on_event("startup")
def on_startup():
    create_db_and_tables()
    run_migrations()


# ---------------------------------------------------------
# Home → redirect to docs
# ---------------------------------------------------------
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


# ---------------------------------------------------------
# Routers
# ---------------------------------------------------------
app.include_router(auth.router)      # /auth/login, require_admin
app.include_router(posts.router)     # /posts/...
app.include_router(texts.router)     # /texts/...
app.include_router(authors.router)   # /authors/...
app.include_router(events.router)    # /events/...
app.include_router(projects.router)  # /projects/...
app.include_router(topics.router)    # /topics/...


# ---------------------------------------------------------
# CORS
# ---------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://vm-social.vercel.app", "https://viewmim.vercel.app"],  
    # allow_origins=["*"],   
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------
# Ready
# ---------------------------------------------------------
print("FastAPI backend running with .env loaded")
