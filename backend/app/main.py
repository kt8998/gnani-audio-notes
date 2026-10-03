from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.routes import recordings

app = FastAPI(title="Audio Notes API")
app.include_router(recordings.router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health(db: Session = Depends(get_db)):
    """Used by Railway's health check and by us to confirm the DB is reachable."""
    db.execute(text("SELECT 1"))
    return {"status": "ok", "database": "ok"}
