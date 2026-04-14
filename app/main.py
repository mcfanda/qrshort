from __future__ import annotations

import io
import base64
import binascii
import hashlib
import hmac
import os
import re
import secrets
import string
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import qrcode
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from psycopg import errors
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
SLUG_ALPHABET = string.ascii_letters + string.digits
SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
SESSION_COOKIE = "qrshort_session"
SESSION_MAX_AGE = 60 * 60 * 12

load_dotenv(PROJECT_ROOT / ".env")


class Settings:
    def __init__(self) -> None:
        self.database_url = os.environ.get("DATABASE_URL", "")
        self.base_url = os.environ.get("BASE_URL", "http://127.0.0.1:8002").rstrip("/")
        self.admin_password = os.environ.get("APP_ADMIN_PASSWORD", "")
        self.create_token = os.environ.get("APP_CREATE_TOKEN", "")
        self.secret_key = os.environ.get("APP_SECRET_KEY", "")

        if not self.database_url:
            raise RuntimeError("DATABASE_URL is required")
        if not self.admin_password:
            raise RuntimeError("APP_ADMIN_PASSWORD is required")
        if not self.secret_key:
            raise RuntimeError("APP_SECRET_KEY is required")


class LinkCreate(BaseModel):
    nickname: str = Field(min_length=1, max_length=160)
    url: str = Field(min_length=1, max_length=4096)
    custom_slug: str | None = Field(default=None, min_length=3, max_length=64)
    replace_existing: bool = False


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=512)


settings = Settings()
pool: ConnectionPool | None = None


def normalize_url(raw_url: str) -> str:
    value = raw_url.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Use a full http or https URL.")
    return value


def normalize_nickname(raw_nickname: str) -> str:
    value = " ".join(raw_nickname.strip().split())
    if not value:
        raise HTTPException(status_code=400, detail="Add a nickname for this URL.")
    return value


def validate_custom_slug(slug: str) -> str:
    value = slug.strip()
    if not SLUG_PATTERN.fullmatch(value):
        raise HTTPException(status_code=400, detail="Use 3-64 letters, numbers, underscores, or hyphens.")
    return value


def make_slug(length: int = 7) -> str:
    return "".join(secrets.choice(SLUG_ALPHABET) for _ in range(length))


def duplicate_message(duplicates: list[dict[str, Any]]) -> str:
    if len(duplicates) == 1:
        return f'"{duplicates[0]["nickname"]}" already uses this nickname or URL.'
    return f"{len(duplicates)} saved QR codes already use this nickname or URL."


def app_pool() -> ConnectionPool:
    if pool is None:
        raise RuntimeError("Database pool is not ready")
    return pool


def short_url_for(slug: str) -> str:
    return f"{settings.base_url}/u/{slug}"


def link_payload(row: dict[str, Any]) -> dict[str, Any]:
    slug = row["slug"]
    return {
        "slug": slug,
        "nickname": row["nickname"] or row["slug"],
        "target_url": row["target_url"],
        "short_url": short_url_for(slug),
        "qr_url": f"{settings.base_url}/qr/{slug}.png",
        "clicks": row["clicks"],
        "created_at": row["created_at"].isoformat(),
        "last_clicked_at": row["last_clicked_at"].isoformat() if row["last_clicked_at"] else None,
    }


def require_create_token(token: str | None) -> None:
    if settings.create_token and not secrets.compare_digest(token or "", settings.create_token):
        raise HTTPException(status_code=401, detail="Missing or invalid create token.")


def sign_session(payload: str) -> str:
    return hmac.new(settings.secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def make_session_cookie() -> str:
    expires = str(int(time.time()) + SESSION_MAX_AGE)
    payload = f"admin:{expires}"
    token = f"{payload}:{sign_session(payload)}"
    return base64.urlsafe_b64encode(token.encode("utf-8")).decode("ascii")


def is_authenticated(request: Request) -> bool:
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return False
    try:
        token = base64.urlsafe_b64decode(cookie.encode("ascii")).decode("utf-8")
        user, expires, signature = token.rsplit(":", 2)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    if user != "admin" or not expires.isdigit() or int(expires) < int(time.time()):
        return False
    return secrets.compare_digest(signature, sign_session(f"{user}:{expires}"))


def require_login(request: Request) -> None:
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Login required.")


def init_db() -> None:
    schema = (PROJECT_ROOT / "schema.sql").read_text(encoding="utf-8")
    with app_pool().connection() as conn:
        conn.execute(schema)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global pool
    pool = ConnectionPool(settings.database_url, min_size=1, max_size=10, kwargs={"row_factory": dict_row})
    init_db()
    yield
    pool.close()


app = FastAPI(title="QR Short URL", lifespan=lifespan)


@app.get("/")
def index(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/links")
def links_page(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(ROOT / "static" / "links.html")


@app.get("/login")
def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse("/", status_code=303)
    return FileResponse(ROOT / "static" / "login.html")


@app.post("/login")
def login(payload: LoginRequest) -> JSONResponse:
    if not secrets.compare_digest(payload.password, settings.admin_password):
        raise HTTPException(status_code=401, detail="Wrong password.")
    response = JSONResponse({"status": "ok"})
    response.set_cookie(
        SESSION_COOKIE,
        make_session_cookie(),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@app.post("/logout")
def logout() -> JSONResponse:
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/health")
def health() -> dict[str, str]:
    with app_pool().connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/api/links")
def create_link(
    payload: LinkCreate,
    request: Request,
    x_create_token: str | None = Header(default=None),
) -> JSONResponse:
    require_login(request)
    require_create_token(x_create_token)
    nickname = normalize_nickname(payload.nickname)
    target_url = normalize_url(payload.url)

    with app_pool().connection() as conn:
        duplicates = conn.execute(
            """
            SELECT slug, nickname, target_url, clicks, created_at, last_clicked_at
            FROM short_links
            WHERE lower(nickname) = lower(%s) OR target_url = %s
            ORDER BY created_at DESC
            """,
            (nickname, target_url),
        ).fetchall()

        if duplicates and not payload.replace_existing:
            duplicate_payloads = [link_payload(row) for row in duplicates]
            return JSONResponse(
                {
                    "detail": "duplicate_link",
                    "message": duplicate_message(duplicate_payloads),
                    "duplicates": duplicate_payloads,
                },
                status_code=409,
            )

        if duplicates and payload.replace_existing:
            duplicate_ids = [row["slug"] for row in duplicates]
            row_to_replace = duplicates[0]
            slug = validate_custom_slug(payload.custom_slug) if payload.custom_slug else row_to_replace["slug"]
            slug_conflict = conn.execute(
                """
                SELECT slug
                FROM short_links
                WHERE slug = %s AND slug <> ALL(%s)
                """,
                (slug, duplicate_ids),
            ).fetchone()
            if slug_conflict:
                raise HTTPException(status_code=409, detail="That short code is already taken.")

            if len(duplicate_ids) > 1:
                conn.execute("DELETE FROM short_links WHERE slug = ANY(%s) AND slug <> %s", (duplicate_ids, row_to_replace["slug"]))

            row = conn.execute(
                """
                UPDATE short_links
                SET slug = %s,
                    nickname = %s,
                    target_url = %s,
                    clicks = 0,
                    created_at = now(),
                    last_clicked_at = NULL
                WHERE slug = %s
                RETURNING slug, nickname, target_url, clicks, created_at, last_clicked_at
                """,
                (slug, nickname, target_url, row_to_replace["slug"]),
            ).fetchone()
            return JSONResponse(link_payload(row), status_code=200)

        if payload.custom_slug:
            slug = validate_custom_slug(payload.custom_slug)
            try:
                row = conn.execute(
                    """
                    INSERT INTO short_links (slug, nickname, target_url)
                    VALUES (%s, %s, %s)
                    RETURNING slug, nickname, target_url, clicks, created_at, last_clicked_at
                    """,
                    (slug, nickname, target_url),
                ).fetchone()
            except errors.UniqueViolation as exc:
                raise HTTPException(status_code=409, detail="That short code is already taken.") from exc
            return JSONResponse(link_payload(row), status_code=201)

        for length in (7, 8, 9, 10):
            for _ in range(8):
                slug = make_slug(length)
                try:
                    row = conn.execute(
                        """
                        INSERT INTO short_links (slug, nickname, target_url)
                        VALUES (%s, %s, %s)
                        RETURNING slug, nickname, target_url, clicks, created_at, last_clicked_at
                        """,
                        (slug, nickname, target_url),
                    ).fetchone()
                    return JSONResponse(link_payload(row), status_code=201)
                except errors.UniqueViolation:
                    continue

    raise HTTPException(status_code=503, detail="Could not create a unique short code. Try again.")


@app.get("/api/links")
def list_links(request: Request) -> list[dict[str, Any]]:
    require_login(request)
    with app_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT slug, nickname, target_url, clicks, created_at, last_clicked_at
            FROM short_links
            ORDER BY created_at DESC
            LIMIT 500
            """
        ).fetchall()
    return [link_payload(row) for row in rows]


@app.get("/api/links/{slug}")
def get_link(slug: str, request: Request) -> dict[str, Any]:
    require_login(request)
    with app_pool().connection() as conn:
        row = conn.execute(
            """
            SELECT slug, nickname, target_url, clicks, created_at, last_clicked_at
            FROM short_links
            WHERE slug = %s
            """,
            (slug,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Short link not found.")
    return link_payload(row)


@app.delete("/api/links/{slug}")
def delete_link(slug: str, request: Request) -> dict[str, str]:
    require_login(request)
    with app_pool().connection() as conn:
        row = conn.execute(
            """
            DELETE FROM short_links
            WHERE slug = %s
            RETURNING slug
            """,
            (slug,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Short link not found.")
    return {"status": "deleted", "slug": row["slug"]}


@app.get("/u/{slug}")
def redirect(slug: str) -> RedirectResponse:
    with app_pool().connection() as conn:
        row = conn.execute(
            """
            UPDATE short_links
            SET clicks = clicks + 1, last_clicked_at = now()
            WHERE slug = %s
            RETURNING target_url
            """,
            (slug,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Short link not found.")
    return RedirectResponse(row["target_url"], status_code=307)


@app.get("/qr/{slug}.png")
def qr_code(slug: str) -> Response:
    with app_pool().connection() as conn:
        row = conn.execute("SELECT slug FROM short_links WHERE slug = %s", (slug,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Short link not found.")

    image = qrcode.make(short_url_for(slug))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return Response(buffer.getvalue(), media_type="image/png")
