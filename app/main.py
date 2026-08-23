from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.routers import auth, history, upload

# DEBUG mode (see app/core/config.py) also raises the root log level so the
# full raw OCR text logged per page in app/services/ocr_extraction.py
# actually reaches the console -- at INFO (the default), only a short
# length summary is logged instead, since raw OCR text can be long.
logging.basicConfig(level=logging.DEBUG if settings.debug_mode else logging.INFO)

app = FastAPI(
    title="AiZen Invoice Extractor",
    description="PDF/Image -> CSV invoice extraction API (Asendus Innovations LLP)",
    version="0.1.0",
)

# Angular dev server origin -- tighten this list once frontend hosting is decided.
# "Authorization" is listed explicitly (not just covered by "*") because the
# Fetch/CORS spec excludes Authorization from wildcard Access-Control-Allow-Headers
# matching -- some browsers won't actually send it cross-origin without this.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4200"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*", "Authorization"],
)

app.include_router(auth.router)
app.include_router(upload.router)
app.include_router(history.router)


@app.get("/health")
async def health_check():
    return {"status": "ok"}
