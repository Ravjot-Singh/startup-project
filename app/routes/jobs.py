import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app import config
from app.db import get_db
from app.jobs import run_tick
from app.whatsapp.provider import get_provider

router = APIRouter()


@router.get("/healthz")
def healthz():
    return {"ok": True}


@router.post("/jobs/tick")
def tick(request: Request, db: Session = Depends(get_db)):
    if not config.TICK_TOKEN:
        raise HTTPException(status_code=404, detail="Not found")
    supplied = request.headers.get("X-Tick-Token", "")
    if not secrets.compare_digest(supplied.encode(), config.TICK_TOKEN.encode()):
        raise HTTPException(status_code=403, detail="Bad tick token")
    return run_tick(db, get_provider())
