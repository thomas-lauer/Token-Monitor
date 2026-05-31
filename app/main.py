"""Token-Monitor FastAPI application entry point.

Mounts:
  /              -> static dashboard (index.html, app.js, styles.css)
  /api/*         -> JSON dashboard endpoints (api.py)
  /v1/metrics    -> OTLP/HTTP metrics receiver
  /v1/logs       -> OTLP/HTTP logs receiver
  /v1/traces    -> OTLP/HTTP traces receiver (accepted, discarded)

A background asyncio task imports new lines from ~/.claude/projects/*.jsonl
every JSONL_POLL_INTERVAL_SECONDS seconds.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import codex_importer, db, jsonl_importer
from .api import router as api_router
from .config import STATIC_DIR
from .otlp_receiver import router as otlp_router


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("token_monitor")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    log.info("Database initialized at %s", db.DB_PATH)
    claude_task = asyncio.create_task(jsonl_importer.importer_loop())
    codex_task = asyncio.create_task(codex_importer.importer_loop())
    log.info("Importer tasks scheduled (Claude + Codex)")
    try:
        yield
    finally:
        for t in (claude_task, codex_task):
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass


app = FastAPI(
    title="Token-Monitor",
    description="Visualize and optimize Claude Code token usage.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(api_router)
app.include_router(otlp_router)

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
