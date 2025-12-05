
from dotenv import load_dotenv
load_dotenv()  
from middleware import auth
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware

from database import create_db_and_tables

# Routers
from routers import posts, texts, authors


app = FastAPI(title="VM Social Timeline API")


# ---------------------------------------------------------
# Startup Event
# ---------------------------------------------------------
@app.on_event("startup")
def on_startup():
    create_db_and_tables()


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


# ---------------------------------------------------------
# CORS
# ---------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://vm-social.vercel.app"],   
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------
# Ready
# ---------------------------------------------------------
print("FastAPI backend running with .env loaded")
