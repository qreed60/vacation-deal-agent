from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db.session import init_db
from app.web.routes import router


app = FastAPI(title="Vacation Deal Agent")
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
app.include_router(router)


@app.on_event("startup")
def startup() -> None:
    init_db()
