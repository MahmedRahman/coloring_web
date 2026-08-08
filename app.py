#!/usr/bin/env python3
"""
Web app: upload a child's photo, choose scenes, get a coloring book.

Run:
    export CLOUDFLARE_ACCOUNT_ID="..."
    export CLOUDFLARE_API_TOKEN="..."
    python3 app.py
"""

from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from functools import wraps
from typing import List, Optional, Union

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import arabic_reshaper
import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from bidi.algorithm import get_display
from authlib.integrations.flask_client import OAuth
from flask import Flask, jsonify, render_template, request, send_file, abort, session, redirect, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as pdf_canvas
from werkzeug.security import check_password_hash, generate_password_hash

from paymob_client import (
    BOOK_PACK_CREDITS,
    BOOK_PACK_PRICE_EGP,
    amount_cents,
    checkout_url,
    create_intention,
    normalize_egypt_phone,
    pay_with_wallet_classic,
    paymob_configured,
    wallet_enabled,
    verify_redirect_hmac,
    verify_transaction_post_hmac,
)
from kie_client import (
    generate_image_to_image,
    kie_configured,
    upload_image as kie_upload_image,
)

ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN")
MODEL = "@cf/black-forest-labs/flux-2-klein-4b"
TRANSLATE_MODEL = "@cf/meta/m2m100-1.2b"
FREE_BOOKS_PER_MONTH = int(os.environ.get("FREE_BOOKS_PER_MONTH", "3"))
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
GOOGLE_CLIENT_ID = (os.environ.get("GOOGLE_CLIENT_ID") or "").strip()
GOOGLE_CLIENT_SECRET = (os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip()
APP_URL = (os.environ.get("APP_URL") or "").rstrip("/")


def google_ready() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def app_base_url() -> str:
    if APP_URL:
        return APP_URL
    return request.url_root.rstrip("/")

# A4 portrait ratio 210:297 — dimensions are multiples of 16 (model-friendly)
# and stay within Workers AI max 1920px
PAGE_WIDTH = 1120
PAGE_HEIGHT = 1584  # exact A4 aspect: 1120/1584 == 210/297
MAX_PAGES = 8
ADMIN_MAX_PAGES = 12

PROMPT_VARIANTS = {
    "a": (
        "black and white line art coloring book page, clean bold outlines, "
        "no shading, no color, no gray, pure white background, "
        "simple children's coloring book illustration style, "
        "vertical A4 portrait page composition, full-page illustration, "
        "keep the exact same child from image 0 — same face, same hairstyle, same age"
    ),
    "b": (
        "black and white line art coloring book page, clean bold outlines, "
        "no shading, no color, no gray, pure white background, "
        "simple children's coloring book illustration style, "
        "vertical A4 portrait page composition, full-page illustration filling the tall page, "
        "identical face to image 0, same facial features, same hairstyle, same age and proportions, "
        "preserve identity from all reference images, recognizable likeness of the same child"
    ),
}
DEFAULT_VARIANT = "b"

LINE_WEIGHT = {
    "thin": "thin delicate outlines",
    "normal": "clean medium-weight outlines",
    "thick": "thick bold heavy outlines",
}
DETAIL_LEVEL = {
    "simple": "very simple shapes, minimal details, easy for toddlers to color",
    "normal": "balanced amount of detail for children",
    "detailed": "richer detailed line work suitable for older children",
}
ART_STYLE = {
    "cartoon": "cute cartoon illustration style",
    "realistic": "semi-realistic proportions and facial features",
}

# Jobs / professions for coloring pages (kept key "scene" for the English prompt text)
SCENES = [
    {"id": "doctor", "emoji": "🩺", "title": "طبيب", "title_en": "Doctor",
     "grad": ["#bfdbfe", "#a7f3d0"],
     "scene": "dressed as a friendly doctor wearing a white coat and stethoscope in a simple clinic"},
    {"id": "engineer", "emoji": "🛠️", "title": "مهندس", "title_en": "Engineer",
     "grad": ["#fed7aa", "#fde68a"],
     "scene": "dressed as a young engineer wearing a hard hat and holding blueprints near simple buildings"},
    {"id": "teacher", "emoji": "📚", "title": "معلم", "title_en": "Teacher",
     "grad": ["#c7d2fe", "#fbcfe8"],
     "scene": "dressed as a teacher standing at a chalkboard with books and an apple"},
    {"id": "pilot", "emoji": "✈️", "title": "طيار", "title_en": "Pilot",
     "grad": ["#bae6fd", "#ddd6fe"],
     "scene": "dressed as an airplane pilot with a captain hat standing near a simple airplane"},
    {"id": "firefighter", "emoji": "🚒", "title": "إطفائي", "title_en": "Firefighter",
     "grad": ["#fecaca", "#fed7aa"],
     "scene": "dressed as a firefighter with a helmet and hose beside a fire truck"},
    {"id": "police", "emoji": "👮", "title": "شرطي", "title_en": "Police officer",
     "grad": ["#bfdbfe", "#e2e8f0"],
     "scene": "dressed as a police officer with a badge and hat standing beside a patrol car"},
    {"id": "chef", "emoji": "👨‍🍳", "title": "طاهي", "title_en": "Chef",
     "grad": ["#fed7aa", "#fecaca"],
     "scene": "dressed as a chef with a tall chef hat cooking in a simple kitchen"},
    {"id": "scientist", "emoji": "🔬", "title": "عالم", "title_en": "Scientist",
     "grad": ["#bbf7d0", "#a5f3fc"],
     "scene": "dressed as a scientist in a lab coat holding a flask in a simple laboratory"},
    {"id": "artist", "emoji": "🎨", "title": "فنان", "title_en": "Artist",
     "grad": ["#fbcfe8", "#fde68a"],
     "scene": "dressed as an artist with a beret painting on an easel with brushes and palette"},
    {"id": "astronaut", "emoji": "🚀", "title": "رائد فضاء", "title_en": "Astronaut",
     "grad": ["#312e81", "#831843"],
     "scene": "dressed as an astronaut in a space suit floating near a rocket and stars"},
    {"id": "soccer", "emoji": "⚽", "title": "لاعب كرة", "title_en": "Soccer player",
     "grad": ["#bbf7d0", "#86efac"],
     "scene": "dressed as a soccer player kicking a ball on a simple soccer field"},
    {"id": "farmer", "emoji": "🌾", "title": "مزارع", "title_en": "Farmer",
     "grad": ["#fde68a", "#86efac"],
     "scene": "dressed as a farmer with a straw hat holding a watering can near simple crops"},
]

FONT_CANDIDATES = [
    "/System/Library/Fonts/SFArabic.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Tahoma.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

CUSTOM_ID_RE = re.compile(r"^custom_[a-f0-9]{8,24}$")
SCENE_ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
SHARE_ID_RE = re.compile(r"^[a-f0-9]{16,64}$")

SESSIONS_DIR = Path(tempfile.gettempdir()) / "coloring_sessions"
SHARES_DIR = Path(tempfile.gettempdir()) / "coloring_shares"
DATA_DIR = Path(os.environ.get("COLORING_DATA_DIR", Path(__file__).resolve().parent / "data"))
DB_PATH = DATA_DIR / "analytics.db"
SPECIAL_ORDERS_DIR = DATA_DIR / "special_orders"
SESSIONS_DIR.mkdir(exist_ok=True)
SHARES_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)
SPECIAL_ORDERS_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 56 * 1024 * 1024  # 56 MB — covers 50 MB PDF uploads
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
if APP_URL.startswith("https://"):
    app.config["SESSION_COOKIE_SECURE"] = True

oauth = OAuth(app)
if google_ready():
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

_db_lock = threading.Lock()
_scheduler: Optional[BackgroundScheduler] = None


@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({
        "error": "وصلت للحد الأقصى: 5 كتب في الساعة. جرّب تاني بعد شوية.",
        "error_en": "Rate limit reached: 5 books per hour. Please try again later.",
    }), 429


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db_lock:
        conn = db_connect()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                book_credits INTEGER DEFAULT 0,
                google_id TEXT UNIQUE,
                auth_provider TEXT DEFAULT 'email'
            );
            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                ip TEXT,
                session_id TEXT,
                pages INTEGER DEFAULT 0,
                user_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS scene_picks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                scene_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                special_reference TEXT NOT NULL UNIQUE,
                amount_cents INTEGER NOT NULL,
                credits INTEGER NOT NULL,
                status TEXT NOT NULL,
                paymob_order_id TEXT,
                paymob_txn_id TEXT,
                created_at TEXT NOT NULL,
                paid_at TEXT
            );
            CREATE TABLE IF NOT EXISTS special_orders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                child_name  TEXT NOT NULL,
                client_name TEXT,
                phone       TEXT,
                email       TEXT,
                notes       TEXT,
                status      TEXT NOT NULL DEFAULT 'pending',
                created_at  TEXT NOT NULL,
                updated_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS special_order_photos (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL REFERENCES special_orders(id) ON DELETE CASCADE,
                filename TEXT NOT NULL
            );
            """
        )
        # Migrate older DBs that lack user_id / book_credits
        cols = {r[1] for r in conn.execute("PRAGMA table_info(books)").fetchall()}
        if "user_id" not in cols:
            conn.execute("ALTER TABLE books ADD COLUMN user_id INTEGER")
        user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "book_credits" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN book_credits INTEGER DEFAULT 0")
        if "google_id" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN google_id TEXT")
        if "auth_provider" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN auth_provider TEXT DEFAULT 'email'")
        # Unique index for google_id (ignore NULLs / duplicates safely)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_id ON users(google_id) "
            "WHERE google_id IS NOT NULL"
        )
        # Migrate: add pdf_filename to special_orders if missing
        so_cols = {r[1] for r in conn.execute("PRAGMA table_info(special_orders)").fetchall()}
        if "pdf_filename" not in so_cols:
            conn.execute("ALTER TABLE special_orders ADD COLUMN pdf_filename TEXT")
        if "assigned_to" not in so_cols:
            conn.execute("ALTER TABLE special_orders ADD COLUMN assigned_to TEXT")
        if "share_token" not in so_cols:
            conn.execute("ALTER TABLE special_orders ADD COLUMN share_token TEXT")
        if "share_expires_at" not in so_cols:
            conn.execute("ALTER TABLE special_orders ADD COLUMN share_expires_at TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_special_orders_share_token "
            "ON special_orders(share_token) WHERE share_token IS NOT NULL"
        )
        conn.commit()
        conn.close()


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_\u0600-\u06FF]{3,30}$")


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    with _db_lock:
        conn = db_connect()
        row = conn.execute(
            "SELECT id, email, username, created_at, book_credits, auth_provider, google_id "
            "FROM users WHERE id = ?",
            (uid,),
        ).fetchone()
        conn.close()
    if not row:
        session.clear()
        return None
    return dict(row)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({
                "error": "لازم تسجّل دخول الأول عشان تولّد الكتاب.",
                "error_en": "Please log in to generate the book.",
                "auth_required": True,
            }), 401
        return view(*args, **kwargs)
    return wrapped


def is_admin() -> bool:
    return bool(session.get("is_admin"))


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_admin():
            wants_json = (
                request.path.startswith("/admin/api")
                or request.path == "/analytics"
                or "application/json" in (request.headers.get("Accept") or "")
            )
            if wants_json:
                return jsonify({
                    "error": "لازم تسجّل دخول الأدمن.",
                    "auth_required": True,
                }), 401
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)
    return wrapped


def scene_title(scene_id: str) -> str:
    for s in SCENES:
        if s["id"] == scene_id:
            return f'{s["emoji"]} {s["title"]}'
    if scene_id.startswith("custom_"):
        return f"✨ مخصص ({scene_id[-8:]})"
    return scene_id


def collect_admin_stats() -> dict:
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    month = now.strftime("%Y-%m")
    week_ago = (now - timedelta(days=7)).isoformat()

    with _db_lock:
        conn = db_connect()
        books_today = conn.execute(
            "SELECT COUNT(*) AS c FROM books WHERE created_at LIKE ?",
            (f"{today}%",),
        ).fetchone()["c"]
        books_yesterday = conn.execute(
            "SELECT COUNT(*) AS c FROM books WHERE created_at >= ? AND created_at < ?",
            ((now - timedelta(days=1)).strftime("%Y-%m-%d"), today),
        ).fetchone()["c"]
        books_week = conn.execute(
            "SELECT COUNT(*) AS c FROM books WHERE created_at >= ?",
            (week_ago,),
        ).fetchone()["c"]
        books_month = conn.execute(
            "SELECT COUNT(*) AS c FROM books WHERE created_at LIKE ?",
            (f"{month}%",),
        ).fetchone()["c"]
        books_total = conn.execute("SELECT COUNT(*) AS c FROM books").fetchone()["c"]
        pages_total = conn.execute(
            "SELECT COALESCE(SUM(pages), 0) AS c FROM books"
        ).fetchone()["c"]
        users_total = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        users_week = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE created_at >= ?",
            (week_ago,),
        ).fetchone()["c"]
        users_today = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE created_at LIKE ?",
            (f"{today}%",),
        ).fetchone()["c"]
        top = conn.execute(
            """
            SELECT scene_id, COUNT(*) AS c
            FROM scene_picks
            GROUP BY scene_id
            ORDER BY c DESC
            LIMIT 12
            """
        ).fetchall()
        recent_books = conn.execute(
            """
            SELECT b.id, b.created_at, b.ip, b.pages, b.session_id,
                   u.username, u.email
            FROM books b
            LEFT JOIN users u ON u.id = b.user_id
            ORDER BY b.id DESC
            LIMIT 25
            """
        ).fetchall()
        recent_users = conn.execute(
            """
            SELECT id, username, email, created_at
            FROM users
            ORDER BY id DESC
            LIMIT 25
            """
        ).fetchall()

        # Last 14 days book counts (fill missing days with 0)
        cutoff_day = (now - timedelta(days=13)).strftime("%Y-%m-%d")
        daily_rows = conn.execute(
            """
            SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS c
            FROM books
            WHERE created_at >= ?
            GROUP BY day
            ORDER BY day
            """,
            (cutoff_day,),
        ).fetchall()
        # Last 14 days revenue (paid payments by paid_at date)
        daily_revenue_rows = conn.execute(
            """
            SELECT substr(paid_at, 1, 10) AS day, COALESCE(SUM(amount_cents), 0) AS c
            FROM payments
            WHERE status = 'paid' AND paid_at >= ?
            GROUP BY day
            ORDER BY day
            """,
            (cutoff_day,),
        ).fetchall()
        # Last 14 days new user signups
        daily_users_rows = conn.execute(
            """
            SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS c
            FROM users
            WHERE created_at >= ?
            GROUP BY day
            ORDER BY day
            """,
            (cutoff_day,),
        ).fetchall()
        conn.close()

    by_day = {r["day"]: int(r["c"]) for r in daily_rows}
    rev_by_day = {r["day"]: int(r["c"]) for r in daily_revenue_rows}
    usr_by_day = {r["day"]: int(r["c"]) for r in daily_users_rows}
    daily = []
    daily_revenue = []
    daily_users = []
    for i in range(13, -1, -1):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        daily.append({"day": d, "label": d[5:], "count": by_day.get(d, 0)})
        daily_revenue.append({
            "day": d,
            "label": d[5:],
            "count": round(rev_by_day.get(d, 0) / 100, 2),
        })
        daily_users.append({"day": d, "label": d[5:], "count": usr_by_day.get(d, 0)})
    max_daily = max((d["count"] for d in daily), default=0) or 1
    max_daily_revenue = max((d["count"] for d in daily_revenue), default=0) or 1
    max_daily_users = max((d["count"] for d in daily_users), default=0) or 1

    sessions_count = sum(1 for p in SESSIONS_DIR.iterdir() if p.is_dir()) if SESSIONS_DIR.exists() else 0
    shares_count = sum(1 for p in SHARES_DIR.glob("*.json")) if SHARES_DIR.exists() else 0
    avg_pages = round((pages_total or 0) / books_total, 1) if books_total else 0

    # Payments stats
    with _db_lock:
        conn = db_connect()
        payments_total = conn.execute("SELECT COUNT(*) AS c FROM payments").fetchone()["c"]
        payments_paid = conn.execute(
            "SELECT COUNT(*) AS c FROM payments WHERE status = 'paid'"
        ).fetchone()["c"]
        revenue_total_cents = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS c FROM payments WHERE status = 'paid'"
        ).fetchone()["c"]
        recent_payments = conn.execute(
            """
            SELECT p.id, p.special_reference, p.amount_cents, p.credits, p.status,
                   p.created_at, p.paid_at, p.paymob_order_id, p.paymob_txn_id,
                   u.username, u.email
            FROM payments p
            LEFT JOIN users u ON u.id = p.user_id
            ORDER BY p.id DESC
            LIMIT 50
            """
        ).fetchall()
        conn.close()

    revenue_egp = round((revenue_total_cents or 0) / 100, 2)
    conversion_rate = round((payments_paid / payments_total * 100), 1) if payments_total else 0

    return {
        "books_today": int(books_today),
        "books_yesterday": int(books_yesterday),
        "books_week": int(books_week),
        "books_month": int(books_month),
        "books_total": int(books_total),
        "pages_total": int(pages_total or 0),
        "avg_pages": avg_pages,
        "users_total": int(users_total),
        "users_week": int(users_week),
        "users_today": int(users_today),
        "sessions_active": sessions_count,
        "shares_active": shares_count,
        "free_books_per_month": FREE_BOOKS_PER_MONTH,
        "generated_at": now.strftime("%Y-%m-%d %H:%M UTC"),
        "daily": daily,
        "max_daily": max_daily,
        "daily_revenue": daily_revenue,
        "max_daily_revenue": max_daily_revenue,
        "daily_users": daily_users,
        "max_daily_users": max_daily_users,
        "top_scenes": [
            {"scene_id": r["scene_id"], "title": scene_title(r["scene_id"]), "count": r["c"]}
            for r in top
        ],
        "recent_books": [dict(r) for r in recent_books],
        "recent_users": [dict(r) for r in recent_users],
        "payments_total": int(payments_total),
        "payments_paid": int(payments_paid),
        "revenue_egp": revenue_egp,
        "conversion_rate": conversion_rate,
        "recent_payments": [dict(r) for r in recent_payments],
    }


def user_quota(user: Optional[dict]) -> dict:
    if not user:
        return {
            "book_credits": 0,
            "free_limit": FREE_BOOKS_PER_MONTH,
            "free_used": 0,
            "free_left": FREE_BOOKS_PER_MONTH,
        }
    credits = get_user_credits(user["id"])
    free_used = monthly_book_count_for_user(user["id"])
    free_left = max(0, FREE_BOOKS_PER_MONTH - free_used)
    return {
        "book_credits": credits,
        "free_limit": FREE_BOOKS_PER_MONTH,
        "free_used": free_used,
        "free_left": free_left,
    }


def user_public(user: dict) -> dict:
    q = user_quota(user)
    return {
        "id": user["id"],
        "email": user["email"],
        "username": user["username"],
        "book_credits": q["book_credits"],
        "auth_provider": user.get("auth_provider") or "email",
        "free_limit": q["free_limit"],
        "free_used": q["free_used"],
        "free_left": q["free_left"],
    }


def _slug_username(raw: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_\u0600-\u06FF]", "", (raw or "").strip())
    if len(cleaned) < 3:
        cleaned = "user" + secrets.token_hex(3)
    return cleaned[:30]


def _next_username(conn: sqlite3.Connection, preferred: str) -> str:
    base = _slug_username(preferred)
    candidate = base
    n = 0
    while True:
        row = conn.execute(
            "SELECT id FROM users WHERE username = ?", (candidate,)
        ).fetchone()
        if not row:
            return candidate
        n += 1
        suffix = str(n)
        candidate = (base[: max(1, 30 - len(suffix))] + suffix)[:30]


def upsert_google_user(google_id: str, email: str, name: str) -> int:
    email = (email or "").strip().lower()
    google_id = (google_id or "").strip()
    if not google_id or not email or not EMAIL_RE.match(email):
        raise ValueError("invalid google profile")

    now = datetime.now(timezone.utc).isoformat()
    with _db_lock:
        conn = db_connect()
        by_google = conn.execute(
            "SELECT id FROM users WHERE google_id = ?", (google_id,)
        ).fetchone()
        if by_google:
            user_id = int(by_google["id"])
            conn.execute(
                "UPDATE users SET auth_provider = CASE "
                "WHEN auth_provider IS NULL OR auth_provider = 'email' THEN 'email+google' "
                "ELSE auth_provider END "
                "WHERE id = ?",
                (user_id,),
            )
            conn.commit()
            conn.close()
            return user_id

        by_email = conn.execute(
            "SELECT id, auth_provider FROM users WHERE email = ?",
            (email,),
        ).fetchone()
        if by_email:
            user_id = int(by_email["id"])
            provider = by_email["auth_provider"] or "email"
            if provider == "email":
                provider = "email+google"
            elif "google" not in provider:
                provider = f"{provider}+google"
            conn.execute(
                "UPDATE users SET google_id = ?, auth_provider = ? WHERE id = ?",
                (google_id, provider, user_id),
            )
            conn.commit()
            conn.close()
            return user_id

        preferred = name or email.split("@")[0]
        last_err: Optional[Exception] = None
        for _ in range(8):
            username = _next_username(conn, preferred)
            try:
                cur = conn.execute(
                    "INSERT INTO users (email, username, password_hash, created_at, "
                    "book_credits, google_id, auth_provider) VALUES (?, ?, '', ?, 0, ?, 'google')",
                    (email, username, now, google_id),
                )
                user_id = int(cur.lastrowid)
                conn.commit()
                conn.close()
                return user_id
            except sqlite3.IntegrityError as e:
                last_err = e
                preferred = (email.split("@")[0] or "user") + secrets.token_hex(2)
                continue
        conn.close()
        raise RuntimeError(f"could not create google user: {last_err}")


def get_user_credits(user_id: int) -> int:
    with _db_lock:
        conn = db_connect()
        row = conn.execute(
            "SELECT book_credits FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        conn.close()
    return int((row["book_credits"] if row else 0) or 0)


def add_user_credits(user_id: int, credits: int):
    with _db_lock:
        conn = db_connect()
        conn.execute(
            "UPDATE users SET book_credits = COALESCE(book_credits, 0) + ? WHERE id = ?",
            (credits, user_id),
        )
        conn.commit()
        conn.close()


def consume_user_credit(user_id: int) -> bool:
    with _db_lock:
        conn = db_connect()
        row = conn.execute(
            "SELECT book_credits FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        credits = int((row["book_credits"] if row else 0) or 0)
        if credits <= 0:
            conn.close()
            return False
        conn.execute(
            "UPDATE users SET book_credits = book_credits - 1 WHERE id = ? AND book_credits > 0",
            (user_id,),
        )
        conn.commit()
        conn.close()
    return True


def monthly_book_count_for_user(user_id: int) -> int:
    month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    with _db_lock:
        conn = db_connect()
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM books WHERE user_id = ? AND created_at LIKE ?",
            (user_id, f"{month_prefix}%"),
        ).fetchone()
        conn.close()
    return int(row["c"] if row else 0)


def collect_user_dashboard(user: dict) -> dict:
    user_id = user["id"]
    month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    with _db_lock:
        conn = db_connect()
        books = conn.execute(
            """
            SELECT id, created_at, pages, session_id
            FROM books
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 50
            """,
            (user_id,),
        ).fetchall()
        books_month = conn.execute(
            "SELECT COUNT(*) AS c FROM books WHERE user_id = ? AND created_at LIKE ?",
            (user_id, f"{month_prefix}%"),
        ).fetchone()["c"]
        books_total = conn.execute(
            "SELECT COUNT(*) AS c FROM books WHERE user_id = ?",
            (user_id,),
        ).fetchone()["c"]
        pages_total = conn.execute(
            "SELECT COALESCE(SUM(pages), 0) AS c FROM books WHERE user_id = ?",
            (user_id,),
        ).fetchone()["c"]
        payments = conn.execute(
            """
            SELECT special_reference, amount_cents, credits, status, created_at, paid_at
            FROM payments
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (user_id,),
        ).fetchall()
        conn.close()

    book_rows = []
    for b in books:
        sid = b["session_id"] or ""
        d = SESSIONS_DIR / sid if sid else None
        available = bool(d and d.exists() and any(d.glob("page_*.jpg")))
        page_files = sorted(d.glob("page_*.jpg")) if available else []
        thumbs = []
        for p in page_files[:4]:
            try:
                thumbs.append(base64.b64encode(p.read_bytes()).decode("ascii"))
            except OSError:
                pass
        book_rows.append({
            "id": b["id"],
            "created_at": b["created_at"],
            "pages": b["pages"],
            "session_id": sid,
            "available": available,
            "thumbs": thumbs,
        })

    credits = get_user_credits(user_id)
    free_used = int(books_month)
    free_left = max(0, FREE_BOOKS_PER_MONTH - free_used)

    return {
        "user": user,
        "credits": credits,
        "books_total": int(books_total),
        "books_month": free_used,
        "pages_total": int(pages_total or 0),
        "free_limit": FREE_BOOKS_PER_MONTH,
        "free_left": free_left,
        "books": book_rows,
        "payments": [dict(p) for p in payments],
        "pack_price": BOOK_PACK_PRICE_EGP,
        "pack_credits": BOOK_PACK_CREDITS,
        "paymob_ready": paymob_configured(),
        "wallet_ready": wallet_enabled(),
    }


def login_required_page(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect(url_for("tool_app", login="1"))
        return view(*args, **kwargs)
    return wrapped


def mark_payment_paid(
    special_reference: str,
    *,
    paymob_order_id: Optional[str] = None,
    paymob_txn_id: Optional[str] = None,
) -> bool:
    """Idempotently mark payment paid and grant credits. Returns True if newly paid."""
    now = datetime.now(timezone.utc).isoformat()
    with _db_lock:
        conn = db_connect()
        row = conn.execute(
            "SELECT * FROM payments WHERE special_reference = ?",
            (special_reference,),
        ).fetchone()
        if not row:
            conn.close()
            return False
        if row["status"] == "paid":
            conn.close()
            return False
        conn.execute(
            """
            UPDATE payments
            SET status = 'paid', paid_at = ?, paymob_order_id = COALESCE(?, paymob_order_id),
                paymob_txn_id = COALESCE(?, paymob_txn_id)
            WHERE special_reference = ? AND status != 'paid'
            """,
            (now, paymob_order_id, paymob_txn_id, special_reference),
        )
        if row["user_id"]:
            conn.execute(
                "UPDATE users SET book_credits = COALESCE(book_credits, 0) + ? WHERE id = ?",
                (int(row["credits"]), row["user_id"]),
            )
        conn.commit()
        conn.close()
    return True


def check_freemium_or_error():
    user = current_user()
    if user and get_user_credits(user["id"]) > 0:
        return None

    if user:
        used = monthly_book_count_for_user(user["id"])
    else:
        used = monthly_book_count(get_remote_address())

    if used >= FREE_BOOKS_PER_MONTH:
        return jsonify({
            "error": f"خلّصت الكتب المجانية لهذا الشهر ({FREE_BOOKS_PER_MONTH} كتب). ادفع عشان تكمل.",
            "error_en": f"Free monthly quota reached ({FREE_BOOKS_PER_MONTH} books). Please pay to continue.",
            "freemium": True,
            "payment_required": True,
            "paymob_ready": paymob_configured(),
            "pack": {
                "price_egp": BOOK_PACK_PRICE_EGP,
                "credits": BOOK_PACK_CREDITS,
            },
        }), 402
    return None


def track_book(session_id: str, pages: int, scene_ids: List[str]) -> dict:
    ip = get_remote_address()
    now = datetime.now(timezone.utc).isoformat()
    user = current_user()
    user_id = user["id"] if user else None
    consumed = None
    # Prefer consuming a paid credit when free monthly quota is already used
    if user_id is not None:
        free_used = monthly_book_count_for_user(user_id)
        if free_used >= FREE_BOOKS_PER_MONTH:
            if consume_user_credit(user_id):
                consumed = "credit"
            else:
                consumed = "credit_failed"
        else:
            consumed = "free"
    with _db_lock:
        conn = db_connect()
        conn.execute(
            "INSERT INTO books (created_at, ip, session_id, pages, user_id) VALUES (?, ?, ?, ?, ?)",
            (now, ip, session_id, pages, user_id),
        )
        for sid in scene_ids:
            conn.execute(
                "INSERT INTO scene_picks (created_at, scene_id) VALUES (?, ?)",
                (now, sid),
            )
        conn.commit()
        conn.close()
    # Refresh user after possible credit consume
    user = current_user()
    return {
        "consumed": consumed,
        "pages": pages,
        "quota": user_quota(user),
    }


def monthly_book_count(ip: str) -> int:
    month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    with _db_lock:
        conn = db_connect()
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM books WHERE ip = ? AND created_at LIKE ?",
            (ip, f"{month_prefix}%"),
        ).fetchone()
        conn.close()
    return int(row["c"] if row else 0)


def build_prompt(
    scene_text: str,
    variant: str = DEFAULT_VARIANT,
    line_weight: str = "normal",
    detail: str = "normal",
    art_style: str = "cartoon",
) -> str:
    style = PROMPT_VARIANTS.get(variant, PROMPT_VARIANTS[DEFAULT_VARIANT])
    extras = ", ".join([
        LINE_WEIGHT.get(line_weight, LINE_WEIGHT["normal"]),
        DETAIL_LEVEL.get(detail, DETAIL_LEVEL["normal"]),
        ART_STYLE.get(art_style, ART_STYLE["cartoon"]),
    ])
    return f"{style}, {extras}, the child is {scene_text}"


def arabic_text(text: str) -> str:
    if not text:
        return text
    return get_display(arabic_reshaper.reshape(text))


def load_font(size: int) -> Union[ImageFont.FreeTypeFont, ImageFont.ImageFont]:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def make_cover_page(child_name: str) -> Image.Image:
    w, h = PAGE_WIDTH, PAGE_HEIGHT
    img = Image.new("RGB", (w, h), "#ffffff")
    draw = ImageDraw.Draw(img)

    for inset, width, color in (
        (48, 6, "#7c3aed"),
        (68, 2, "#ec4899"),
        (88, 1, "#c4b5fd"),
    ):
        draw.rectangle([inset, inset, w - inset, h - inset], outline=color, width=width)

    ornament = 28
    for x, y in ((120, 120), (w - 120, 120), (120, h - 120), (w - 120, h - 120)):
        draw.ellipse([x - ornament, y - ornament, x + ornament, y + ornament], outline="#f59e0b", width=3)

    title_font = load_font(72)
    name_font = load_font(56)
    date_font = load_font(32)
    subtitle_font = load_font(28)

    title = arabic_text("كتاب تلوين")
    subtitle = arabic_text("مولّد خصيصًا لطفلك")
    name = arabic_text(child_name.strip() or "طفلي")
    today = arabic_text(date.today().strftime("%Y/%m/%d"))

    def center_text(text, font, y, fill):
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        draw.text(((w - tw) / 2, y), text, font=font, fill=fill)

    cy = h // 2
    center_text(title, title_font, cy - 160, "#7c3aed")
    center_text(subtitle, subtitle_font, cy - 60, "#7a7480")
    center_text(name, name_font, cy + 40, "#1f1b24")
    center_text(today, date_font, cy + 140, "#7a7480")
    return img


def friendly_error(exc: Exception) -> str:
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError, TimeoutError)):
        return "انتهى وقت الانتظار. السيرفر متأخر — جرّب تاني."
    if isinstance(exc, httpx.ConnectError):
        return "مفيش اتصال بالإنترنت أو خدمة التوليد. تأكد من الشبكة وحاول مرة أخرى."
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code if exc.response is not None else None
        if status in (401, 403):
            return "مفاتيح الخدمة غير صحيحة أو منتهية. راجع التوكن."
        if status == 429:
            return "خدمة التوليد مشغولة دلوقتي. استنى دقيقة وجرّب تاني."
        if status and status >= 500:
            return "خدمة التوليد فيها مشكلة مؤقتة. جرّب بعد شوية."
        return "فشل طلب التوليد. جرّب مرة أخرى."
    msg = str(exc)
    # Pass through already-localized Kie / app errors
    if msg.startswith("Kie.ai") or msg.startswith("مفتاح") or msg.startswith("رصيد"):
        return msg
    if "API error" in msg:
        return "الموديل رفض الطلب. جرّب صورة أوضح أو موقف تاني."
    return "حصل خطأ غير متوقع أثناء التوليد. جرّب مرة أخرى."


def session_dir(session_id: str) -> Path:
    if not session_id.isalnum() or len(session_id) > 40:
        abort(400, "Bad session id")
    d = SESSIONS_DIR / session_id
    if not d.exists():
        abort(404, "Session not found")
    return d


def ref_image_paths(d: Path) -> List[Path]:
    paths = []
    for i in range(4):
        p = d / f"input_{i}.png"
        if p.exists():
            paths.append(p)
    if not paths:
        legacy = d / "input.png"
        if legacy.exists():
            paths.append(legacy)
    return paths


def ensure_multi_refs(d: Path) -> List[Path]:
    paths = ref_image_paths(d)
    if len(paths) == 1:
        img = Image.open(paths[0]).convert("RGB")
        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        cropped = img.crop((left, top, left + side, top + side)).resize(
            (min(side, 1024), min(side, 1024))
        )
        second = d / "input_1.png"
        cropped.save(second, "PNG")
        paths.append(second)
    return paths


def validate_portrait_image(img: Image.Image) -> Optional[str]:
    """Soft validation: reject tiny/blank-ish images (not a full face detector)."""
    w, h = img.size
    if w < 200 or h < 200:
        return "الصورة صغيرة جدًا. استخدم صورة أوضح للوجه (200×200 على الأقل)."
    # Reject near-solid images (very low variance)
    small = img.convert("L").resize((64, 64))
    hist = small.histogram()
    nonzero = sum(1 for v in hist if v > 0)
    if nonzero < 8:
        return "الصورة تبدو فارغة أو بلون واحد. ارفع صورة واضحة لوجه الطفل."
    return None


def scene_by_id(scene_id: str, d: Optional[Path] = None):
    for s in SCENES:
        if s["id"] == scene_id:
            return s
    if d and CUSTOM_ID_RE.match(scene_id):
        meta = d / f"{scene_id}.json"
        if meta.exists():
            return json.loads(meta.read_text(encoding="utf-8"))
    return None


def page_path(d: Path, scene_id: str) -> Path:
    return d / f"page_{scene_id}.jpg"


def style_from_request(data: Optional[dict] = None):
    data = data or {}
    args = request.args
    return {
        "variant": data.get("variant") or args.get("variant") or DEFAULT_VARIANT,
        "line_weight": data.get("line_weight") or args.get("line_weight") or "normal",
        "detail": data.get("detail") or args.get("detail") or "normal",
        "art_style": data.get("art_style") or args.get("art_style") or "cartoon",
    }


async def call_model_async(
    prompt: str,
    image_paths: List[Path],
    client: httpx.AsyncClient,
) -> bytes:
    url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/run/{MODEL}"
    headers = {"Authorization": f"Bearer {API_TOKEN}"}
    last_exc: Optional[Exception] = None

    for attempt in range(2):
        try:
            files = []
            handles = []
            try:
                for i, path in enumerate(image_paths[:4]):
                    fh = open(path, "rb")
                    handles.append(fh)
                    files.append((f"input_image_{i}", (path.name, fh, "image/png")))
                data = {
                    "prompt": prompt,
                    "width": str(PAGE_WIDTH),
                    "height": str(PAGE_HEIGHT),
                }
                resp = await client.post(url, headers=headers, data=data, files=files)
            finally:
                for fh in handles:
                    fh.close()

            resp.raise_for_status()
            payload = resp.json()
            if not payload.get("success", False):
                raise RuntimeError(f"API error: {payload.get('errors')}")
            return base64.b64decode(payload["result"]["image"])
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_exc = e
            if attempt == 0:
                await asyncio.sleep(1.5)
                continue
            raise
    raise last_exc  # pragma: no cover


async def translate_ar_to_en(text: str, client: httpx.AsyncClient) -> str:
    url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/run/{TRANSLATE_MODEL}"
    headers = {"Authorization": f"Bearer {API_TOKEN}", "Content-Type": "application/json"}
    resp = await client.post(
        url,
        headers=headers,
        json={"text": text, "source_lang": "ar", "target_lang": "en"},
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success", False):
        raise RuntimeError(f"API error: {payload.get('errors')}")
    result = payload.get("result") or {}
    translated = result.get("translated_text") or result.get("translatedText") or ""
    if not translated:
        raise RuntimeError("Empty translation")
    return translated.strip()


async def generate_one_async(
    d: Path,
    scene: dict,
    force: bool,
    style: dict,
    client: httpx.AsyncClient,
) -> dict:
    scene_id = scene["id"]
    out = page_path(d, scene_id)
    created = False
    if force and out.exists():
        out.unlink()
    if not out.exists():
        refs = ensure_multi_refs(d)
        prompt = build_prompt(
            scene["scene"],
            variant=style.get("variant", DEFAULT_VARIANT),
            line_weight=style.get("line_weight", "normal"),
            detail=style.get("detail", "normal"),
            art_style=style.get("art_style", "cartoon"),
        )
        img_bytes = await call_model_async(prompt, refs, client)
        Image.open(io.BytesIO(img_bytes)).convert("RGB").save(out, "JPEG", quality=90)
        created = True
    b64 = base64.b64encode(out.read_bytes()).decode()
    return {
        "scene_id": scene_id,
        "title": scene.get("title", scene_id),
        "title_en": scene.get("title_en", scene.get("title", scene_id)),
        "emoji": scene.get("emoji", "✨"),
        "image_b64": b64,
        "created": created,
    }


async def generate_one_kie_async(
    d: Path,
    scene: dict,
    force: bool,
    style: dict,
    client: httpx.AsyncClient,
    *,
    ref_url: Optional[str] = None,
    sem: Optional[asyncio.Semaphore] = None,
) -> dict:
    """Generate one coloring page via Kie.ai GPT Image 2 (image-to-image)."""
    scene_id = scene["id"]
    out = page_path(d, scene_id)
    created = False
    if force and out.exists():
        out.unlink()
    if not out.exists():
        refs = ensure_multi_refs(d)
        prompt = build_prompt(
            scene["scene"],
            variant=style.get("variant", DEFAULT_VARIANT),
            line_weight=style.get("line_weight", "normal"),
            detail=style.get("detail", "normal"),
            art_style=style.get("art_style", "cartoon"),
        )
        # Stronger identity preservation for GPT Image 2
        prompt = (
            f"{prompt}. "
            "This is image-to-image: keep the exact same child face, hair, age and identity "
            "from the reference photo, converted to simple black-and-white coloring book line art only."
        )
        ref_path = refs[0]

        async def _work():
            img_bytes, _ = await generate_image_to_image(
                prompt, ref_path, client, input_url=ref_url
            )
            Image.open(io.BytesIO(img_bytes)).convert("RGB").save(out, "JPEG", quality=92)

        if sem is not None:
            async with sem:
                await _work()
        else:
            await _work()
        created = True
    b64 = base64.b64encode(out.read_bytes()).decode()
    return {
        "scene_id": scene_id,
        "title": scene.get("title", scene_id),
        "title_en": scene.get("title_en", scene.get("title", scene_id)),
        "emoji": scene.get("emoji", "✨"),
        "image_b64": b64,
        "created": created,
        "provider": "kie",
    }


def run_async(coro):
    return asyncio.run(coro)


def write_pdf_with_margins(images: List[Image.Image], pdf_path: Path):
    """Build A4 PDF — pages fill the sheet (images are already A4 ratio)."""
    page_w, page_h = A4
    # Small print margin (~5mm) so artwork nearly fills A4
    margin = 14
    footer_h = 16
    c = pdf_canvas.Canvas(str(pdf_path), pagesize=A4)
    total = len(images)
    for idx, img in enumerate(images, start=1):
        usable_w = page_w - 2 * margin
        usable_h = page_h - 2 * margin - footer_h
        iw, ih = img.size
        scale = min(usable_w / iw, usable_h / ih)
        draw_w, draw_h = iw * scale, ih * scale
        x = (page_w - draw_w) / 2
        y = margin + footer_h + (usable_h - draw_h) / 2
        c.drawImage(
            ImageReader(img), x, y,
            width=draw_w, height=draw_h,
            preserveAspectRatio=True, mask="auto",
        )
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.45, 0.45, 0.45)
        c.drawCentredString(page_w / 2, 8, f"{idx} / {total}")
        c.showPage()
    c.save()


def cleanup_old_sessions(max_age_hours: int = 24):
    cutoff = time.time() - max_age_hours * 3600
    for folder in (SESSIONS_DIR, SHARES_DIR):
        if not folder.exists():
            continue
        for item in folder.iterdir():
            try:
                if item.stat().st_mtime < cutoff:
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink(missing_ok=True)
            except OSError:
                pass
    # Expire share metadata
    for meta in SHARES_DIR.glob("*.json"):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            exp = datetime.fromisoformat(data.get("expires_at", "1970-01-01T00:00:00+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp < datetime.now(timezone.utc):
                pdf = SHARES_DIR / f"{meta.stem}.pdf"
                pdf.unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
        except Exception:
            pass


def start_scheduler():
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(cleanup_old_sessions, "interval", hours=1, id="cleanup")
    _scheduler.start()
    cleanup_old_sessions()


init_db()
start_scheduler()


@app.route("/dashboard")
@login_required_page
def user_dashboard():
    user = current_user()
    data = collect_user_dashboard(user)
    return render_template("dashboard.html", **data)


@app.route("/")
def index():
    return render_template("landing.html")


@app.route("/app")
def tool_app():
    return render_template(
        "app.html",
        scenes=SCENES,
        free_books=FREE_BOOKS_PER_MONTH,
        book_pack_price=BOOK_PACK_PRICE_EGP,
        book_pack_credits=BOOK_PACK_CREDITS,
        paymob_ready=paymob_configured(),
        wallet_ready=wallet_enabled(),
        google_ready=google_ready(),
    )


@app.route("/auth/register", methods=["POST"])
@limiter.limit("10 per hour")
def auth_register():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not EMAIL_RE.match(email):
        return jsonify({"error": "الإيميل مش صالح."}), 400
    if not USERNAME_RE.match(username):
        return jsonify({"error": "اسم المستخدم لازم 3–30 حرف (حروف/أرقام/_)."}), 400
    if len(password) < 6:
        return jsonify({"error": "كلمة المرور لازم 6 حروف على الأقل."}), 400

    now = datetime.now(timezone.utc).isoformat()
    pw_hash = generate_password_hash(password)
    try:
        with _db_lock:
            conn = db_connect()
            cur = conn.execute(
                "INSERT INTO users (email, username, password_hash, created_at, auth_provider) "
                "VALUES (?, ?, ?, ?, 'email')",
                (email, username, pw_hash, now),
            )
            user_id = cur.lastrowid
            conn.commit()
            conn.close()
    except sqlite3.IntegrityError:
        return jsonify({"error": "الإيميل أو اسم المستخدم مستخدم قبل كده."}), 409

    session.clear()
    session["user_id"] = user_id
    session.permanent = True
    return jsonify({
        "ok": True,
        "user": {"id": user_id, "email": email, "username": username},
    })


@app.route("/auth/login", methods=["POST"])
@limiter.limit("20 per hour")
def auth_login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if not email or not password:
        return jsonify({"error": "اكتب الإيميل وكلمة المرور."}), 400

    with _db_lock:
        conn = db_connect()
        row = conn.execute(
            "SELECT id, email, username, password_hash, book_credits, auth_provider "
            "FROM users WHERE email = ?",
            (email,),
        ).fetchone()
        conn.close()

    if not row:
        return jsonify({"error": "الإيميل أو كلمة المرور غلط."}), 401

    pw_hash = row["password_hash"] or ""
    if not pw_hash:
        return jsonify({
            "error": "الحساب ده متسجل بجوجل. دوس على «المتابعة مع Google».",
            "error_en": "This account uses Google. Continue with Google.",
        }), 401
    if not check_password_hash(pw_hash, password):
        return jsonify({"error": "الإيميل أو كلمة المرور غلط."}), 401

    session.clear()
    session["user_id"] = row["id"]
    session.permanent = True
    return jsonify({"ok": True, "user": user_public(dict(row))})


@app.route("/auth/google")
@limiter.limit("30 per hour")
def auth_google_start():
    if not google_ready():
        return redirect(url_for("tool_app", login="1", auth_error="google_off"))
    redirect_uri = f"{app_base_url()}/auth/google/callback"
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/auth/google/callback")
def auth_google_callback():
    if not google_ready():
        return redirect(url_for("tool_app", login="1", auth_error="google_off"))
    try:
        token = oauth.google.authorize_access_token()
    except Exception:
        return redirect(url_for("tool_app", login="1", auth_error="google_failed"))

    info = token.get("userinfo") if isinstance(token, dict) else None
    if not info:
        try:
            info = oauth.google.userinfo(token=token)
        except Exception:
            info = None
    if not info:
        return redirect(url_for("tool_app", login="1", auth_error="google_failed"))

    google_id = str(info.get("sub") or "").strip()
    email = (info.get("email") or "").strip().lower()
    name = (info.get("name") or info.get("given_name") or "").strip()
    if not info.get("email_verified", True):
        return redirect(url_for("tool_app", login="1", auth_error="google_unverified"))

    try:
        user_id = upsert_google_user(google_id, email, name)
    except Exception:
        return redirect(url_for("tool_app", login="1", auth_error="google_failed"))

    session.clear()
    session["user_id"] = user_id
    session.permanent = True
    return redirect(url_for("tool_app"))


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/auth/me")
def auth_me():
    user = current_user()
    if not user:
        return jsonify({"authenticated": False, "user": None})
    return jsonify({"authenticated": True, "user": user_public(user)})


@app.route("/upload", methods=["POST"])
@limiter.limit("5 per hour")
@login_required
def upload():
    freemium = check_freemium_or_error()
    if freemium:
        return freemium

    files = request.files.getlist("photos") or []
    if not files:
        single = request.files.get("photo")
        if single:
            files = [single]
    files = [f for f in files if f and f.filename][:4]
    if not files:
        return jsonify({"error": "مفيش صورة مرفوعة. اختار صورة وحاول تاني."}), 400

    session_id = secrets.token_hex(12)
    d = SESSIONS_DIR / session_id
    d.mkdir()
    try:
        for i, f in enumerate(files):
            img = Image.open(f.stream).convert("RGB")
            err = validate_portrait_image(img)
            if err and i == 0:
                shutil.rmtree(d, ignore_errors=True)
                return jsonify({"error": err}), 400
            # Cap stored resolution
            max_side = 1600
            if max(img.size) > max_side:
                img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            img.save(d / f"input_{i}.png", "PNG")
        (d / "input.png").write_bytes((d / "input_0.png").read_bytes())
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        return jsonify({"error": "الصورة مش صالحة. جرّب JPG أو PNG."}), 400

    ensure_multi_refs(d)
    return jsonify({"session_id": session_id, "refs": len(ref_image_paths(d))})


@app.route("/custom-scene/<session_id>", methods=["POST"])
@login_required
def create_custom_scene(session_id: str):
    d = session_dir(session_id)
    data = request.get_json(silent=True) or {}
    arabic = (data.get("text") or "").strip()[:120]
    if len(arabic) < 3:
        return jsonify({"error": "اكتب وصف الموقف بالعربية (٣ حروف على الأقل)."}), 400

    async def _translate():
        async with httpx.AsyncClient(timeout=60.0) as client:
            return await translate_ar_to_en(arabic, client)

    try:
        english = run_async(_translate())
    except Exception as e:
        return jsonify({"error": friendly_error(e)}), 500

    scene_id = f"custom_{secrets.token_hex(6)}"
    scene = {
        "id": scene_id,
        "emoji": "✨",
        "title": arabic[:40],
        "title_en": english[:40],
        "grad": ["#e9d5ff", "#fbcfe8"],
        "scene": english,
        "custom": True,
    }
    (d / f"{scene_id}.json").write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")
    return jsonify(scene)


@app.route("/generate/<session_id>/<scene_id>")
@login_required
def generate_page(session_id: str, scene_id: str):
    d = session_dir(session_id)
    if not SCENE_ID_RE.match(scene_id):
        return jsonify({"error": "الموقف مش موجود."}), 400
    scene = scene_by_id(scene_id, d)
    if not scene:
        return jsonify({"error": "الموقف مش موجود."}), 400

    force = request.args.get("force") in ("1", "true", "yes")
    style = style_from_request()

    async def _run():
        async with httpx.AsyncClient(timeout=180.0) as client:
            return await generate_one_async(d, scene, force, style, client)

    try:
        result = run_async(_run())
    except Exception as e:
        return jsonify({"error": friendly_error(e)}), 500
    return jsonify(result)


@app.route("/generate-batch/<session_id>", methods=["POST"])
@login_required
def generate_batch(session_id: str):
    freemium = check_freemium_or_error()
    if freemium:
        return freemium
    d = session_dir(session_id)
    data = request.get_json(silent=True) or {}
    scene_ids = data.get("scenes") or []
    force = bool(data.get("force"))
    style = style_from_request(data)
    if not scene_ids:
        return jsonify({"error": "مفيش وظائف محددة."}), 400
    if len(scene_ids) > MAX_PAGES:
        return jsonify({"error": f"أقصى عدد للصفحات هو {MAX_PAGES}."}), 400

    scenes = []
    for sid in scene_ids:
        if not isinstance(sid, str) or not SCENE_ID_RE.match(sid):
            return jsonify({"error": "موقف غير صالح."}), 400
        scene = scene_by_id(sid, d)
        if not scene:
            return jsonify({"error": f"الموقف مش موجود: {sid}"}), 400
        scenes.append(scene)

    async def _run_all():
        async with httpx.AsyncClient(timeout=180.0) as client:
            tasks = [generate_one_async(d, sc, force, style, client) for sc in scenes]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            out = []
            for sc, res in zip(scenes, results):
                if isinstance(res, Exception):
                    out.append({"scene_id": sc["id"], "error": friendly_error(res)})
                else:
                    out.append(res)
            return out

    try:
        pages = run_async(_run_all())
    except Exception as e:
        return jsonify({"error": friendly_error(e)}), 500

    ok_ids = [p["scene_id"] for p in pages if not p.get("error")]
    created_ids = [p["scene_id"] for p in pages if not p.get("error") and p.get("created")]
    usage = None
    if created_ids:
        usage = track_book(session_id, len(created_ids), created_ids)
    elif ok_ids:
        # Restored/cached pages — no new consumption
        usage = {
            "consumed": "none",
            "pages": len(ok_ids),
            "quota": user_quota(current_user()),
        }
    return jsonify({"pages": pages, "usage": usage})


@app.route("/session/<session_id>")
def get_session(session_id: str):
    d = session_dir(session_id)
    pages = []
    customs = []
    for meta in sorted(d.glob("custom_*.json")):
        try:
            customs.append(json.loads(meta.read_text(encoding="utf-8")))
        except Exception:
            pass
    for p in sorted(d.glob("page_*.jpg")):
        sid = p.stem[len("page_"):]
        scene = scene_by_id(sid, d) or {"id": sid, "title": sid, "emoji": "🎨"}
        pages.append({
            "scene_id": sid,
            "title": scene.get("title", sid),
            "title_en": scene.get("title_en", scene.get("title", sid)),
            "emoji": scene.get("emoji", "🎨"),
            "image_b64": base64.b64encode(p.read_bytes()).decode(),
        })
    return jsonify({
        "session_id": session_id,
        "refs": len(ref_image_paths(d)),
        "pages": pages,
        "custom_scenes": customs,
    })


@app.route("/pdf/<session_id>")
@limiter.limit("5 per hour")
@login_required
def build_pdf(session_id: str):
    d = session_dir(session_id)
    order = request.args.get("order", "").split(",")
    child_name = (request.args.get("name") or "").strip()[:40]
    pages = []
    used_ids = []
    for sid in order:
        sid = sid.strip()
        if not sid or not SCENE_ID_RE.match(sid):
            continue
        p = page_path(d, sid)
        if p.exists():
            pages.append(Image.open(p).convert("RGB"))
            used_ids.append(sid)
    if not pages:
        abort(400, "No pages generated yet")

    cover = make_cover_page(child_name)
    all_pages = [cover] + pages
    pdf_path = d / "coloring_book.pdf"
    write_pdf_with_margins(all_pages, pdf_path)
    return send_file(pdf_path, as_attachment=True, download_name="coloring_book.pdf")


@app.route("/share/<session_id>", methods=["POST"])
@limiter.limit("10 per hour")
@login_required
def share_book(session_id: str):
    """Create a temporary share link (local storage, 24h). R2 optional later via env."""
    d = session_dir(session_id)
    data = request.get_json(silent=True) or {}
    order = data.get("order") or []
    child_name = (data.get("name") or "").strip()[:40]
    if not order:
        return jsonify({"error": "مفيش صفحات للمشاركة."}), 400

    pages = []
    for sid in order:
        if not isinstance(sid, str) or not SCENE_ID_RE.match(sid):
            continue
        p = page_path(d, sid)
        if p.exists():
            pages.append(Image.open(p).convert("RGB"))
    if not pages:
        return jsonify({"error": "مفيش صفحات جاهزة."}), 400

    cover = make_cover_page(child_name)
    token = secrets.token_hex(16)
    pdf_path = SHARES_DIR / f"{token}.pdf"
    write_pdf_with_margins([cover] + pages, pdf_path)
    expires = datetime.now(timezone.utc) + timedelta(hours=24)
    meta = {"token": token, "expires_at": expires.isoformat(), "session_id": session_id}
    (SHARES_DIR / f"{token}.json").write_text(json.dumps(meta), encoding="utf-8")
    url = f"{request.host_url.rstrip('/')}/s/{token}"
    return jsonify({"url": url, "expires_at": expires.isoformat()})


@app.route("/s/<token>")
def get_share(token: str):
    if not SHARE_ID_RE.match(token):
        abort(404)
    meta_path = SHARES_DIR / f"{token}.json"
    pdf_path = SHARES_DIR / f"{token}.pdf"
    if not meta_path.exists() or not pdf_path.exists():
        abort(404)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    exp = datetime.fromisoformat(meta["expires_at"])
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < datetime.now(timezone.utc):
        pdf_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        abort(410)
    return send_file(pdf_path, as_attachment=True, download_name="coloring_book.pdf")


@app.route("/pay/create", methods=["POST"])
@login_required
@limiter.limit("20 per hour")
def pay_create():
    if not paymob_configured():
        return jsonify({"error": "الدفع مش متفعّل حاليًا. تواصل مع الإدارة."}), 503

    user = current_user()
    data = request.get_json(silent=True) or {}
    phone_raw = (data.get("phone") or "").strip()
    phone = normalize_egypt_phone(phone_raw)
    preferred = (data.get("method") or "all").strip().lower()
    if preferred not in ("all", "card", "wallet"):
        preferred = "all"

    # Explicit wallet uses classic CASH API (Intention API doesn't support it)
    if preferred == "wallet":
        if not wallet_enabled():
            return jsonify({"error": "المحفظة لسة مش متفعّلة على الحساب."}), 503
        digits = phone.replace("+", "")
        if not phone_raw or not digits.startswith("20") or len(digits) < 12:
            return jsonify({
                "error": "اكتب رقم موبايل المحفظة المصري (مثال: 010xxxxxxxx).",
            }), 400

        special_reference = f"pack_{user['id']}_{secrets.token_hex(8)}"
        now = datetime.now(timezone.utc).isoformat()
        with _db_lock:
            conn = db_connect()
            conn.execute(
                """
                INSERT INTO payments (user_id, special_reference, amount_cents, credits, status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (user["id"], special_reference, amount_cents(), BOOK_PACK_CREDITS, now),
            )
            conn.commit()
            conn.close()

        base = os.environ.get("APP_URL", request.url_root.rstrip("/"))
        try:
            result = pay_with_wallet_classic(
                special_reference=special_reference,
                phone=phone_raw or phone,
                customer={
                    "first_name": (user.get("username") or "Customer")[:40],
                    "last_name": "User",
                    "email": user.get("email") or "customer@example.com",
                },
                redirection_url=f"{base}/pay/complete",
            )
        except Exception as e:
            with _db_lock:
                conn = db_connect()
                conn.execute(
                    "UPDATE payments SET status = 'failed' WHERE special_reference = ?",
                    (special_reference,),
                )
                conn.commit()
                conn.close()
            return jsonify({"error": friendly_error(e)}), 502

        with _db_lock:
            conn = db_connect()
            conn.execute(
                "UPDATE payments SET paymob_order_id = ?, paymob_txn_id = ? WHERE special_reference = ?",
                (result.get("order_id"), result.get("txn_id"), special_reference),
            )
            conn.commit()
            conn.close()

        if result.get("success") and not result.get("pending"):
            mark_payment_paid(
                special_reference,
                paymob_order_id=result.get("order_id"),
                paymob_txn_id=result.get("txn_id"),
            )
            return jsonify({
                "ok": True,
                "flow": "wallet",
                "reference": special_reference,
                "checkout_url": f"{base}/pay/complete?success=true&merchant_order_id={special_reference}&order={result.get('order_id')}&id={result.get('txn_id')}&amount_cents={amount_cents()}&pending=false",
                "amount_egp": BOOK_PACK_PRICE_EGP,
                "credits": BOOK_PACK_CREDITS,
            })

        if result.get("redirect_url"):
            return jsonify({
                "ok": True,
                "flow": "wallet",
                "reference": special_reference,
                "checkout_url": result["redirect_url"],
                "amount_egp": BOOK_PACK_PRICE_EGP,
                "credits": BOOK_PACK_CREDITS,
            })

        msg = result.get("message") or "الدفع بالمحفظة فشل."
        # Common when CASH integration is created but not fully activated by Paymob
        if "something went wrong" in msg.lower() or not msg:
            msg = (
                "تكامل المحفظة اتعمل، بس Paymob لسة مش مفعّلاه بالكامل على الحساب. "
                "كلّم دعم Paymob وقولهم فعّلوا Mobile Wallet / CASH على Integration "
                f"{os.environ.get('PAYMOB_INTEGRATION_ID_WALLET', '')}."
            )
        with _db_lock:
            conn = db_connect()
            conn.execute(
                "UPDATE payments SET status = 'failed' WHERE special_reference = ?",
                (special_reference,),
            )
            conn.commit()
            conn.close()
        return jsonify({"error": msg, "flow": "wallet"}), 502

    # Card / all → Unified Checkout (card integration only)
    if preferred == "all":
        preferred = "card"

    special_reference = f"pack_{user['id']}_{secrets.token_hex(8)}"
    now = datetime.now(timezone.utc).isoformat()

    with _db_lock:
        conn = db_connect()
        conn.execute(
            """
            INSERT INTO payments (user_id, special_reference, amount_cents, credits, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (user["id"], special_reference, amount_cents(), BOOK_PACK_CREDITS, now),
        )
        conn.commit()
        conn.close()

    base = os.environ.get("APP_URL", request.url_root.rstrip("/"))
    try:
        intention = create_intention(
            special_reference=special_reference,
            customer={
                "first_name": (user.get("username") or "Customer")[:40],
                "last_name": "User",
                "email": user.get("email") or "customer@example.com",
                "phone": phone,
            },
            notification_url=f"{base}/pay/webhook",
            redirection_url=f"{base}/pay/complete",
            preferred_method=preferred,
        )
    except Exception as e:
        with _db_lock:
            conn = db_connect()
            conn.execute(
                "UPDATE payments SET status = 'failed' WHERE special_reference = ?",
                (special_reference,),
            )
            conn.commit()
            conn.close()
        return jsonify({"error": friendly_error(e)}), 502

    client_secret = intention.get("client_secret")
    if not client_secret:
        return jsonify({"error": "Paymob مرجوعش client_secret."}), 502

    order_id = intention.get("intention_order_id") or intention.get("id")
    with _db_lock:
        conn = db_connect()
        conn.execute(
            "UPDATE payments SET paymob_order_id = ? WHERE special_reference = ?",
            (str(order_id) if order_id is not None else None, special_reference),
        )
        conn.commit()
        conn.close()

    return jsonify({
        "ok": True,
        "flow": "card",
        "reference": special_reference,
        "checkout_url": checkout_url(client_secret),
        "amount_egp": BOOK_PACK_PRICE_EGP,
        "credits": BOOK_PACK_CREDITS,
    })


@app.route("/pay/webhook", methods=["POST"])
def pay_webhook():
    body = request.get_json(silent=True) or {}
    obj = body.get("obj") or {}
    received = request.args.get("hmac", "")
    if not verify_transaction_post_hmac(obj, received):
        return jsonify({"error": "Invalid HMAC"}), 401

    if obj.get("success") and not obj.get("pending"):
        special = (
            obj.get("merchant_order_id")
            or (obj.get("order") or {}).get("merchant_order_id")
            or ""
        )
        # Fallback: match by paymob order id
        if not special:
            order_id = str((obj.get("order") or {}).get("id") or "")
            with _db_lock:
                conn = db_connect()
                row = conn.execute(
                    "SELECT special_reference FROM payments WHERE paymob_order_id = ?",
                    (order_id,),
                ).fetchone()
                conn.close()
            special = row["special_reference"] if row else ""
        if special:
            mark_payment_paid(
                special,
                paymob_order_id=str((obj.get("order") or {}).get("id") or ""),
                paymob_txn_id=str(obj.get("id") or ""),
            )
    return jsonify({"received": True})


@app.route("/pay/complete")
def pay_complete():
    args = {k: request.args.get(k, "") for k in request.args}
    success = str(args.get("success", "")).lower() in ("true", "1")
    pending = str(args.get("pending", "")).lower() in ("true", "1")
    special = args.get("merchant_order_id") or args.get("merchant_order") or ""
    amount_egp = None
    try:
        if args.get("amount_cents"):
            amount_egp = int(args["amount_cents"]) / 100
    except ValueError:
        amount_egp = BOOK_PACK_PRICE_EGP

    if not special and args.get("order"):
        with _db_lock:
            conn = db_connect()
            row = conn.execute(
                "SELECT special_reference FROM payments WHERE paymob_order_id = ?",
                (args.get("order"),),
            ).fetchone()
            conn.close()
        special = row["special_reference"] if row else ""

    hmac_ok = verify_redirect_hmac(args) if args.get("hmac") else False
    status = "unknown"
    book_credits = None

    if success and not pending and special and hmac_ok:
        mark_payment_paid(
            special,
            paymob_order_id=args.get("order"),
            paymob_txn_id=args.get("id"),
        )
        status = "success"
    elif success and special:
        # Already paid (wallet classic flow) or waiting for webhook
        with _db_lock:
            conn = db_connect()
            row = conn.execute(
                "SELECT status FROM payments WHERE special_reference = ?",
                (special,),
            ).fetchone()
            conn.close()
        if row and row["status"] == "paid":
            status = "success"
        elif not args.get("hmac"):
            # Our internal wallet success redirect (no Paymob hmac)
            if row and row["status"] == "paid":
                status = "success"
            else:
                status = "pending"
        else:
            status = "pending"
    elif args and not success:
        status = "failed"

    user = current_user()
    if user:
        book_credits = get_user_credits(user["id"])

    return render_template(
        "pay_complete.html",
        status=status,
        reference=special,
        credits=BOOK_PACK_CREDITS,
        price=amount_egp if amount_egp is not None else BOOK_PACK_PRICE_EGP,
        txn_id=args.get("id") or "",
        order_id=args.get("order") or "",
        card_last4=args.get("source_data.pan") or args.get("source_data_pan") or "",
        card_brand=args.get("source_data.sub_type") or args.get("source_data_sub_type") or "",
        book_credits=book_credits,
        hmac_ok=hmac_ok,
    )


@app.route("/pay/status/<reference>")
def pay_status(reference: str):
    with _db_lock:
        conn = db_connect()
        row = conn.execute(
            "SELECT status, credits, amount_cents, paid_at, user_id FROM payments WHERE special_reference = ?",
            (reference,),
        ).fetchone()
        conn.close()
    if not row:
        return jsonify({"error": "الطلب مش موجود."}), 404
    user = current_user()
    credits_now = get_user_credits(user["id"]) if user else None
    return jsonify({
        "status": row["status"],
        "credits": row["credits"],
        "amount_egp": (row["amount_cents"] or 0) / 100,
        "paid_at": row["paid_at"],
        "book_credits": credits_now,
    })


@app.route("/analytics")
@admin_required
def analytics():
    stats = collect_admin_stats()
    return jsonify({
        "books_today": stats["books_today"],
        "books_week": stats["books_week"],
        "books_month": stats["books_month"],
        "books_total": stats["books_total"],
        "pages_total": stats["pages_total"],
        "users_total": stats["users_total"],
        "top_scenes": stats["top_scenes"],
        "free_books_per_month": stats["free_books_per_month"],
    })


@app.route("/admin/login", methods=["GET", "POST"])
@limiter.limit("30 per hour")
def admin_login():
    if is_admin():
        return redirect(url_for("admin_dashboard"))

    error = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        # compare_digest requires equal length; mismatch → False without raising
        user_ok = (
            len(username) == len(ADMIN_USERNAME)
            and secrets.compare_digest(username, ADMIN_USERNAME)
        )
        pass_ok = (
            len(password) == len(ADMIN_PASSWORD)
            and secrets.compare_digest(password, ADMIN_PASSWORD)
        )
        if user_ok and pass_ok:
            session["is_admin"] = True
            session.permanent = True
            return redirect(url_for("admin_dashboard"))
        error = "اسم المستخدم أو كلمة المرور غلط."

    return render_template("admin_login.html", error=error)


@app.route("/admin/logout", methods=["POST", "GET"])
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    stats = collect_admin_stats()
    return render_template("admin.html", stats=stats, scenes=SCENES)


@app.route("/admin/api/quick-book/upload", methods=["POST"])
@admin_required
def admin_quick_book_upload():
    """Admin: upload child photo for quick book (no freemium limits)."""
    f = request.files.get("photo") or (request.files.getlist("photos") or [None])[0]
    if not f or not f.filename:
        return jsonify({"error": "ارفع صورة الطفل."}), 400

    session_id = secrets.token_hex(12)
    d = SESSIONS_DIR / session_id
    d.mkdir()
    try:
        img = Image.open(f.stream).convert("RGB")
        err = validate_portrait_image(img)
        if err:
            shutil.rmtree(d, ignore_errors=True)
            return jsonify({"error": err}), 400
        max_side = 1600
        if max(img.size) > max_side:
            img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        img.save(d / "input_0.png", "PNG")
        (d / "input.png").write_bytes((d / "input_0.png").read_bytes())
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        return jsonify({"error": "الصورة مش صالحة. جرّب JPG أو PNG."}), 400

    ensure_multi_refs(d)
    return jsonify({"ok": True, "session_id": session_id})


@app.route("/admin/api/quick-book/generate", methods=["POST"])
@admin_required
def admin_quick_book_generate():
    """Admin: generate coloring pages via Kie.ai GPT Image 2 (no freemium)."""
    if not kie_configured():
        return jsonify({
            "error": "مفتاح Kie.ai مش مضبوط. أضف KIE_API_KEY في ملف .env وأعد تشغيل السيرفر.",
        }), 500

    data = request.get_json(silent=True) or {}
    session_id = (data.get("session_id") or "").strip()
    if not session_id.isalnum() or len(session_id) > 40:
        return jsonify({"error": "session غير صالح."}), 400
    d = SESSIONS_DIR / session_id
    if not d.exists():
        return jsonify({"error": "الجلسة مش موجودة. ارفع الصورة تاني."}), 404

    scene_ids = data.get("scenes") or []
    if not isinstance(scene_ids, list) or not scene_ids:
        return jsonify({"error": "اختار وظيفة واحدة على الأقل."}), 400
    if len(scene_ids) > ADMIN_MAX_PAGES:
        return jsonify({"error": f"أقصى عدد للصفحات هو {ADMIN_MAX_PAGES}."}), 400

    force = bool(data.get("force"))
    style = {
        "variant": data.get("variant") or DEFAULT_VARIANT,
        "line_weight": data.get("line_weight") or "normal",
        "detail": data.get("detail") or "normal",
        "art_style": data.get("art_style") or "cartoon",
    }

    scenes = []
    for sid in scene_ids:
        if not isinstance(sid, str) or not SCENE_ID_RE.match(sid):
            return jsonify({"error": "وظيفة غير صالحة."}), 400
        scene = scene_by_id(sid, d)
        if not scene:
            return jsonify({"error": f"الوظيفة مش موجودة: {sid}"}), 400
        scenes.append(scene)

    refs = ensure_multi_refs(d)
    if not refs:
        return jsonify({"error": "مفيش صورة مرجع. ارفع صورة الطفل تاني."}), 400

    async def _run_all():
        # Longer timeout: each GPT Image task can take ~60s
        timeout = httpx.Timeout(60.0, read=300.0, write=120.0, connect=30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            ref_url = await kie_upload_image(
                refs[0],
                client,
                upload_path="coloring-book",
                file_name=f"{session_id}.png",
            )
            # Cache ref URL on disk for retries in same session
            try:
                (d / "kie_ref_url.txt").write_text(ref_url, encoding="utf-8")
            except OSError:
                pass

            sem = asyncio.Semaphore(3)  # limit parallel Kie tasks
            tasks = [
                generate_one_kie_async(
                    d, sc, force, style, client, ref_url=ref_url, sem=sem
                )
                for sc in scenes
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            out = []
            for sc, res in zip(scenes, results):
                if isinstance(res, Exception):
                    out.append({"scene_id": sc["id"], "error": friendly_error(res)})
                else:
                    out.append(res)
            return out

    try:
        pages = run_async(_run_all())
    except Exception as e:
        return jsonify({"error": friendly_error(e)}), 500

    ok_ids = [p["scene_id"] for p in pages if not p.get("error")]
    created_ids = [p["scene_id"] for p in pages if not p.get("error") and p.get("created")]
    if created_ids:
        ip = get_remote_address()
        now = datetime.now(timezone.utc).isoformat()
        with _db_lock:
            conn = db_connect()
            conn.execute(
                "INSERT INTO books (created_at, ip, session_id, pages, user_id) VALUES (?, ?, ?, ?, ?)",
                (now, ip, session_id, len(created_ids), None),
            )
            for sid in created_ids:
                conn.execute(
                    "INSERT INTO scene_picks (created_at, scene_id) VALUES (?, ?)",
                    (now, sid),
                )
            conn.commit()
            conn.close()

    return jsonify({
        "ok": True,
        "provider": "kie",
        "model": "gpt-image-2-image-to-image",
        "pages": pages,
        "ok_count": len(ok_ids),
        "failed": [p for p in pages if p.get("error")],
    })


@app.route("/admin/api/quick-book/pdf/<session_id>")
@admin_required
def admin_quick_book_pdf(session_id: str):
    """Admin: download generated PDF for a quick-book session."""
    if not session_id.isalnum() or len(session_id) > 40:
        abort(400, "Bad session")
    d = SESSIONS_DIR / session_id
    if not d.exists():
        abort(404, "Session not found")

    order = request.args.get("order", "").split(",")
    child_name = (request.args.get("name") or "").strip()[:40]
    pages = []
    for sid in order:
        sid = sid.strip()
        if not sid or not SCENE_ID_RE.match(sid):
            continue
        p = page_path(d, sid)
        if p.exists():
            pages.append(Image.open(p).convert("RGB"))
    if not pages:
        return jsonify({"error": "مفيش صفحات جاهزة. ولّد الكتاب الأول."}), 400

    cover = make_cover_page(child_name)
    pdf_path = d / "coloring_book.pdf"
    write_pdf_with_margins([cover] + pages, pdf_path)
    safe_name = re.sub(r"[^\w\u0600-\u06FF\-]+", "_", child_name or "coloring_book")[:40]
    download_name = f"{safe_name or 'coloring_book'}.pdf"
    return send_file(pdf_path, as_attachment=True, download_name=download_name)


@app.route("/admin/api/stats")
@admin_required
def admin_api_stats():
    return jsonify(collect_admin_stats())


@app.route("/admin/api/user/<int:user_id>/credits", methods=["POST"])
@admin_required
def admin_user_credits(user_id: int):
    """Add or subtract credits for a user. JSON body: {delta: int, note: str}"""
    data = request.get_json(silent=True) or {}
    delta = data.get("delta")
    if delta is None or not isinstance(delta, int):
        return jsonify({"error": "delta مطلوب وهو عدد صحيح (موجب = إضافة، سالب = خصم)."}), 400
    if delta == 0:
        return jsonify({"error": "delta لازم يكون غير صفر."}), 400
    if abs(delta) > 1000:
        return jsonify({"error": "الحد الأقصى 1000 credit في المرة."}), 400

    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT id, username, book_credits FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "المستخدم مش موجود."}), 404
        # Prevent going below 0 on deduction
        current = int((row["book_credits"] or 0))
        new_credits = max(0, current + delta)
        conn.execute("UPDATE users SET book_credits = ? WHERE id = ?", (new_credits, user_id))
        conn.commit()
        conn.close()

    return jsonify({
        "ok": True,
        "user_id": user_id,
        "delta": delta,
        "new_credits": new_credits,
    })


@app.route("/admin/api/user/<int:user_id>", methods=["DELETE"])
@admin_required
def admin_delete_user(user_id: int):
    """Delete a user and their associated books/payments."""
    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "المستخدم مش موجود."}), 404
        username = row["username"]
        conn.execute("DELETE FROM books WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM payments WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        conn.close()
    return jsonify({"ok": True, "deleted_user": username})


@app.route("/admin/api/payments")
@admin_required
def admin_payments():
    """Return paginated payments list."""
    page = max(1, int(request.args.get("page", 1)))
    per_page = 50
    offset = (page - 1) * per_page
    with _db_lock:
        conn = db_connect()
        total = conn.execute("SELECT COUNT(*) AS c FROM payments").fetchone()["c"]
        rows = conn.execute(
            """
            SELECT p.id, p.special_reference, p.amount_cents, p.credits, p.status,
                   p.created_at, p.paid_at, p.paymob_order_id, p.paymob_txn_id,
                   u.username, u.email
            FROM payments p
            LEFT JOIN users u ON u.id = p.user_id
            ORDER BY p.id DESC
            LIMIT ? OFFSET ?
            """,
            (per_page, offset),
        ).fetchall()
        conn.close()
    return jsonify({
        "total": int(total),
        "page": page,
        "per_page": per_page,
        "payments": [dict(r) for r in rows],
    })


@app.route("/admin/api/users")
@admin_required
def admin_users_search():
    """Search users by username/email. Empty q → latest 50."""
    q = (request.args.get("q") or "").strip()[:80]
    with _db_lock:
        conn = db_connect()
        if q:
            like = f"%{q}%"
            rows = conn.execute(
                """
                SELECT id, username, email, created_at, book_credits, auth_provider
                FROM users
                WHERE username LIKE ? OR email LIKE ?
                ORDER BY id DESC
                LIMIT 100
                """,
                (like, like),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, username, email, created_at, book_credits, auth_provider
                FROM users
                ORDER BY id DESC
                LIMIT 50
                """
            ).fetchall()
        conn.close()
    return jsonify({"total": len(rows), "users": [dict(r) for r in rows]})


def _csv_download(filename: str, header: list, rows: list):
    """Build a CSV download response (BOM for correct Arabic in Excel)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return app.response_class(
        "﻿" + buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/admin/api/export/users.csv")
@admin_required
def admin_export_users():
    with _db_lock:
        conn = db_connect()
        rows = conn.execute(
            "SELECT id, username, email, created_at, book_credits, auth_provider "
            "FROM users ORDER BY id"
        ).fetchall()
        conn.close()
    return _csv_download(
        "users.csv",
        ["id", "username", "email", "created_at", "book_credits", "auth_provider"],
        [tuple(r) for r in rows],
    )


@app.route("/admin/api/export/books.csv")
@admin_required
def admin_export_books():
    with _db_lock:
        conn = db_connect()
        rows = conn.execute(
            """
            SELECT b.id, b.created_at, b.pages, b.ip, b.session_id,
                   u.username, u.email
            FROM books b
            LEFT JOIN users u ON u.id = b.user_id
            ORDER BY b.id
            """
        ).fetchall()
        conn.close()
    return _csv_download(
        "books.csv",
        ["id", "created_at", "pages", "ip", "session_id", "username", "email"],
        [tuple(r) for r in rows],
    )


@app.route("/admin/api/export/payments.csv")
@admin_required
def admin_export_payments():
    with _db_lock:
        conn = db_connect()
        rows = conn.execute(
            """
            SELECT p.id, p.special_reference, p.amount_cents, p.credits, p.status,
                   p.created_at, p.paid_at, p.paymob_order_id, p.paymob_txn_id,
                   u.username, u.email
            FROM payments p
            LEFT JOIN users u ON u.id = p.user_id
            ORDER BY p.id
            """
        ).fetchall()
        conn.close()
    return _csv_download(
        "payments.csv",
        ["id", "special_reference", "amount_cents", "credits", "status",
         "created_at", "paid_at", "paymob_order_id", "paymob_txn_id",
         "username", "email"],
        [tuple(r) for r in rows],
    )


@app.route("/admin/api/user/<int:user_id>/notify", methods=["POST"])
@admin_required
def admin_notify_user(user_id: int):
    """Store a notification message in the DB for the user to see on next login."""
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()[:500]
    if len(message) < 5:
        return jsonify({"error": "الرسالة قصيرة جداً (5 حروف على الأقل)."}), 400
    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "المستخدم مش موجود."}), 404
        # Store notification in a simple JSON column; create table if needed
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS admin_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                read_at TEXT
            );
            """
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO admin_notifications (user_id, message, created_at) VALUES (?, ?, ?)",
            (user_id, message, now_iso),
        )
        conn.commit()
        conn.close()
    return jsonify({"ok": True, "user_id": user_id, "message": message})


@app.route("/admin/api/user/<int:user_id>")
@admin_required
def admin_get_user(user_id: int):
    """Get detailed info for a single user including books and payments."""
    with _db_lock:
        conn = db_connect()
        user = conn.execute(
            "SELECT id, username, email, created_at, book_credits, auth_provider FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if not user:
            conn.close()
            return jsonify({"error": "المستخدم مش موجود."}), 404
        books_count = conn.execute(
            "SELECT COUNT(*) AS c FROM books WHERE user_id = ?", (user_id,)
        ).fetchone()["c"]
        payments_rows = conn.execute(
            "SELECT special_reference, amount_cents, credits, status, created_at, paid_at "
            "FROM payments WHERE user_id = ? ORDER BY id DESC LIMIT 10",
            (user_id,),
        ).fetchall()
        conn.close()
    return jsonify({
        "user": dict(user),
        "books_count": int(books_count),
        "payments": [dict(r) for r in payments_rows],
    })


# ─────────────────────────── Special Orders (WhatsApp) ───────────────────────────

ALLOWED_PHOTO_EXT = {"jpg", "jpeg", "png", "webp", "heic", "heif"}
MAX_PHOTOS_PER_ORDER = 8
MAX_PHOTO_SIZE = 10 * 1024 * 1024  # 10 MB


def _order_photo_dir(order_id: int) -> Path:
    d = SPECIAL_ORDERS_DIR / str(order_id)
    d.mkdir(exist_ok=True)
    return d


def _safe_photo_name(filename: str) -> Optional[str]:
    """Return a safe filename or None if extension not allowed."""
    name = Path(filename).name
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in ALLOWED_PHOTO_EXT:
        return None
    return f"{secrets.token_hex(8)}.{ext}"


def _order_share_active(token: Optional[str], expires_raw: Optional[str]) -> bool:
    if not token or not expires_raw:
        return False
    try:
        exp = datetime.fromisoformat(expires_raw)
    except ValueError:
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp > datetime.now(timezone.utc)


def _order_to_dict(row: sqlite3.Row, photos: list) -> dict:
    d = dict(row)
    d["photos"] = photos
    # pdf_filename / assigned_to / share fields may not exist in older rows
    for key in ("pdf_filename", "assigned_to", "share_token", "share_expires_at"):
        if key not in d:
            d[key] = None
    token = d.get("share_token")
    d["share_active"] = _order_share_active(token, d.get("share_expires_at"))
    d["share_url"] = f"{app_base_url()}/so/{token}" if token else None
    return d


@app.route("/admin/api/special-orders", methods=["GET"])
@admin_required
def admin_list_special_orders():
    """List special orders. Supports ?status=pending|done and ?q= / ?client=<search>."""
    status_filter = request.args.get("status", "").strip().lower()
    # Accept both ?q= and legacy ?client=
    search = (
        request.args.get("q") or request.args.get("client") or ""
    ).strip()
    with _db_lock:
        conn = db_connect()
        conditions = []
        params: list = []
        if status_filter in ("pending", "done"):
            conditions.append("status = ?")
            params.append(status_filter)
        if search:
            like = f"%{search}%"
            conditions.append(
                "("
                "CAST(id AS TEXT) LIKE ? OR "
                "child_name LIKE ? OR "
                "client_name LIKE ? OR "
                "phone LIKE ? OR "
                "email LIKE ? OR "
                "assigned_to LIKE ? OR "
                "notes LIKE ?"
                ")"
            )
            params.extend([like] * 7)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = conn.execute(
            f"SELECT * FROM special_orders {where} ORDER BY id DESC",
            params,
        ).fetchall()
        photo_rows = conn.execute(
            "SELECT order_id, filename FROM special_order_photos"
        ).fetchall()
        conn.close()

    photos_by_order: dict = {}
    for p in photo_rows:
        photos_by_order.setdefault(p["order_id"], []).append(p["filename"])

    orders = [_order_to_dict(r, photos_by_order.get(r["id"], [])) for r in rows]
    return jsonify({"orders": orders, "total": len(orders)})


@app.route("/admin/api/special-orders/<int:order_id>", methods=["GET"])
@admin_required
def admin_get_special_order(order_id: int):
    """Get single special order details with photos."""
    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT * FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "الطلب مش موجود."}), 404
        photo_rows = conn.execute(
            "SELECT filename FROM special_order_photos WHERE order_id = ?", (order_id,)
        ).fetchall()
        conn.close()
    photos = [r["filename"] for r in photo_rows]
    return jsonify({"ok": True, "order": _order_to_dict(row, photos)})


@app.route("/admin/api/special-orders", methods=["POST"])
@admin_required
def admin_create_special_order():
    """Create a new special order. JSON body (child_name required)."""
    data = request.get_json(silent=True) or {}
    child_name = (data.get("child_name") or "").strip()
    if not child_name:
        return jsonify({"error": "اسم الطفل مطلوب."}), 400

    now = datetime.now(timezone.utc).isoformat()
    with _db_lock:
        conn = db_connect()
        cur = conn.execute(
            """
            INSERT INTO special_orders
              (child_name, client_name, phone, email, notes, status, assigned_to, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                child_name,
                (data.get("client_name") or "").strip() or None,
                (data.get("phone") or "").strip() or None,
                (data.get("email") or "").strip() or None,
                (data.get("notes") or "").strip() or None,
                data.get("status", "pending") if data.get("status") in ("pending", "done") else "pending",
                (data.get("assigned_to") or "").strip() or None,
                now,
            ),
        )
        order_id = cur.lastrowid
        conn.commit()
        row = conn.execute("SELECT * FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        conn.close()
    return jsonify({"ok": True, "order": _order_to_dict(row, [])}), 201


@app.route("/admin/api/special-orders/<int:order_id>", methods=["PUT"])
@admin_required
def admin_update_special_order(order_id: int):
    """Update fields of an existing special order."""
    data = request.get_json(silent=True) or {}
    now = datetime.now(timezone.utc).isoformat()

    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT * FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "الطلب مش موجود."}), 404

        child_name  = (data.get("child_name") or row["child_name"]).strip()
        client_name = (data.get("client_name") if "client_name" in data else row["client_name"])
        phone       = (data.get("phone")       if "phone"       in data else row["phone"])
        email       = (data.get("email")       if "email"       in data else row["email"])
        notes       = (data.get("notes")       if "notes"       in data else row["notes"])
        status      = (data.get("status")      if "status"      in data else row["status"])
        assigned_to = (data.get("assigned_to") if "assigned_to" in data else row["assigned_to"])
        if status not in ("pending", "done"):
            status = row["status"]

        conn.execute(
            """
            UPDATE special_orders
            SET child_name=?, client_name=?, phone=?, email=?, notes=?, status=?, assigned_to=?, updated_at=?
            WHERE id=?
            """,
            (child_name, client_name or None, phone or None, email or None,
             notes or None, status, assigned_to or None, now, order_id),
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        photos  = [r["filename"] for r in conn.execute(
            "SELECT filename FROM special_order_photos WHERE order_id = ?", (order_id,)
        ).fetchall()]
        conn.close()
    return jsonify({"ok": True, "order": _order_to_dict(updated, photos)})


@app.route("/admin/api/special-orders/<int:order_id>", methods=["DELETE"])
@admin_required
def admin_delete_special_order(order_id: int):
    """Delete a special order and all its photos from disk."""
    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT id FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "الطلب مش موجود."}), 404
        conn.execute("DELETE FROM special_order_photos WHERE order_id = ?", (order_id,))
        conn.execute("DELETE FROM special_orders WHERE id = ?", (order_id,))
        conn.commit()
        conn.close()
    # Remove entire order directory (photos + pdf)
    import shutil as _shutil
    _shutil.rmtree(SPECIAL_ORDERS_DIR / str(order_id), ignore_errors=True)
    return jsonify({"ok": True})


@app.route("/admin/api/special-orders/<int:order_id>/photos", methods=["POST"])
@admin_required
def admin_upload_order_photos(order_id: int):
    """Upload one or more photos for an order (multipart/form-data, field: photos)."""
    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT id FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        existing_count = conn.execute(
            "SELECT COUNT(*) AS c FROM special_order_photos WHERE order_id = ?", (order_id,)
        ).fetchone()["c"]
        conn.close()
    if not row:
        return jsonify({"error": "الطلب مش موجود."}), 404
    if existing_count >= MAX_PHOTOS_PER_ORDER:
        return jsonify({"error": f"أقصى عدد صور هو {MAX_PHOTOS_PER_ORDER}."}), 400

    files = request.files.getlist("photos")
    if not files:
        return jsonify({"error": "مفيش صور مرفوعة."}), 400

    slots_left = MAX_PHOTOS_PER_ORDER - existing_count
    saved = []
    photo_dir = _order_photo_dir(order_id)

    for f in files[:slots_left]:
        if not f or not f.filename:
            continue
        safe_name = _safe_photo_name(f.filename)
        if not safe_name:
            continue
        dest = photo_dir / safe_name
        f.seek(0, 2)
        size = f.tell()
        f.seek(0)
        if size > MAX_PHOTO_SIZE:
            continue
        try:
            img = Image.open(f.stream).convert("RGB")
            # Cap at 1600px
            if max(img.size) > 1600:
                img.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            # Save as JPEG regardless of original format with max quality
            jpeg_name = safe_name.rsplit(".", 1)[0] + ".jpg"
            img.save(photo_dir / jpeg_name, "JPEG", quality=95)
            saved.append(jpeg_name)
        except Exception:
            continue

    if not saved:
        return jsonify({"error": "مفيش صور صالحة تم رفعها."}), 400

    with _db_lock:
        conn = db_connect()
        for name in saved:
            conn.execute(
                "INSERT INTO special_order_photos (order_id, filename) VALUES (?, ?)",
                (order_id, name),
            )
        conn.commit()
        conn.close()

    return jsonify({"ok": True, "uploaded": saved}), 201


@app.route("/admin/special-orders/<int:order_id>/photo/<filename>")
@admin_required
def admin_serve_order_photo(order_id: int, filename: str):
    """Serve a single photo file for a special order."""
    # Sanitize filename — only alphanumeric + dot + underscore + hyphen
    if not re.match(r'^[a-zA-Z0-9_\-]+\.[a-zA-Z]+$', filename):
        abort(404)
    photo_path = SPECIAL_ORDERS_DIR / str(order_id) / filename
    if not photo_path.exists():
        abort(404)
    return send_file(photo_path)


@app.route("/admin/api/special-orders/<int:order_id>/photo/<filename>", methods=["DELETE"])
@admin_required
def admin_delete_order_photo(order_id: int, filename: str):
    """Delete a single photo from a special order."""
    if not re.match(r'^[a-zA-Z0-9_\-]+\.[a-zA-Z]+$', filename):
        return jsonify({"error": "اسم الملف غير صالح."}), 400
    photo_path = SPECIAL_ORDERS_DIR / str(order_id) / filename
    with _db_lock:
        conn = db_connect()
        conn.execute(
            "DELETE FROM special_order_photos WHERE order_id = ? AND filename = ?",
            (order_id, filename),
        )
        conn.commit()
        conn.close()
    photo_path.unlink(missing_ok=True)
    return jsonify({"ok": True})


# ─── PDF endpoints ───────────────────────────────────────────────────────────

MAX_PDF_SIZE = 50 * 1024 * 1024  # 50 MB


@app.route("/admin/api/special-orders/<int:order_id>/pdf", methods=["POST"])
@admin_required
def admin_upload_order_pdf(order_id: int):
    """Upload (or replace) the PDF for a special order."""
    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT id, pdf_filename FROM special_orders WHERE id = ?",
                           (order_id,)).fetchone()
        conn.close()
    if not row:
        return jsonify({"error": "الطلب مش موجود."}), 404

    f = request.files.get("pdf")
    if not f or not f.filename:
        return jsonify({"error": "مفيش ملف PDF مرفوع."}), 400

    ext = Path(f.filename).suffix.lower()
    if ext != ".pdf":
        return jsonify({"error": "الملف لازم يكون PDF."}), 400

    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    if size > MAX_PDF_SIZE:
        return jsonify({"error": f"حجم الملف أكبر من {MAX_PDF_SIZE // 1024 // 1024} MB."}), 400

    pdf_dir  = _order_photo_dir(order_id)  # same directory as photos
    pdf_name = f"order_{order_id}_{secrets.token_hex(6)}.pdf"
    pdf_path = pdf_dir / pdf_name

    # Delete old PDF if exists
    old_pdf = row["pdf_filename"]
    if old_pdf:
        (pdf_dir / old_pdf).unlink(missing_ok=True)

    f.save(str(pdf_path))

    with _db_lock:
        conn = db_connect()
        conn.execute(
            "UPDATE special_orders SET pdf_filename=?, updated_at=? WHERE id=?",
            (pdf_name, datetime.now(timezone.utc).isoformat(), order_id),
        )
        conn.commit()
        conn.close()

    return jsonify({"ok": True, "pdf_filename": pdf_name}), 201


@app.route("/admin/special-orders/<int:order_id>/pdf/<filename>")
@admin_required
def admin_serve_order_pdf(order_id: int, filename: str):
    """Serve the PDF file for download."""
    if not re.match(r'^[a-zA-Z0-9_\-]+\.pdf$', filename):
        abort(404)
    pdf_path = SPECIAL_ORDERS_DIR / str(order_id) / filename
    if not pdf_path.exists():
        abort(404)
    return send_file(pdf_path, as_attachment=True, download_name=filename)


@app.route("/admin/api/special-orders/<int:order_id>/pdf", methods=["DELETE"])
@admin_required
def admin_delete_order_pdf(order_id: int):
    """Delete the PDF attached to a special order."""
    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT pdf_filename FROM special_orders WHERE id=?",
                           (order_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "الطلب مش موجود."}), 404
        old_pdf = row["pdf_filename"]
        conn.execute(
            "UPDATE special_orders SET pdf_filename=NULL, share_token=NULL, "
            "share_expires_at=NULL, updated_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), order_id),
        )
        conn.commit()
        conn.close()
    if old_pdf:
        (SPECIAL_ORDERS_DIR / str(order_id) / old_pdf).unlink(missing_ok=True)
    return jsonify({"ok": True})


# ─── Public share links for special-order PDFs ───────────────────────────────

ORDER_SHARE_DAYS = int(os.environ.get("ORDER_SHARE_DAYS", "7"))


@app.route("/admin/api/special-orders/<int:order_id>/share", methods=["POST"])
@admin_required
def admin_create_order_share(order_id: int):
    """Create (or regenerate) a public share link for the order's PDF."""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=ORDER_SHARE_DAYS)
    token = secrets.token_hex(16)
    with _db_lock:
        conn = db_connect()
        row = conn.execute(
            "SELECT id, pdf_filename FROM special_orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "الطلب مش موجود."}), 404
        if not row["pdf_filename"]:
            conn.close()
            return jsonify({"error": "ارفع ملف PDF الأول قبل إنشاء رابط مشاركة."}), 400
        conn.execute(
            "UPDATE special_orders SET share_token=?, share_expires_at=?, updated_at=? WHERE id=?",
            (token, expires.isoformat(), now.isoformat(), order_id),
        )
        conn.commit()
        conn.close()
    return jsonify({
        "ok": True,
        "url": f"{app_base_url()}/so/{token}",
        "token": token,
        "expires_at": expires.isoformat(),
    }), 201


@app.route("/admin/api/special-orders/<int:order_id>/share", methods=["DELETE"])
@admin_required
def admin_revoke_order_share(order_id: int):
    """Revoke the public share link of an order."""
    now = datetime.now(timezone.utc).isoformat()
    with _db_lock:
        conn = db_connect()
        row = conn.execute("SELECT id FROM special_orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "الطلب مش موجود."}), 404
        conn.execute(
            "UPDATE special_orders SET share_token=NULL, share_expires_at=NULL, updated_at=? WHERE id=?",
            (now, order_id),
        )
        conn.commit()
        conn.close()
    return jsonify({"ok": True})


def _norm_order_phone(raw: str) -> str:
    """Normalize order phone for sibling matching (Egypt-friendly)."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("0020"):
        digits = digits[2:]
    if digits.startswith("20") and len(digits) > 11:
        digits = digits[2:]
    if digits.startswith("0"):
        digits = digits[1:]
    return digits[-10:] if len(digits) >= 10 else digits


@app.route("/so/<token>")
@limiter.limit("60 per hour")
def get_order_share(token: str):
    """Public (no-auth) branded share page: view the order's PDF + siblings' PDFs."""
    if not SHARE_ID_RE.match(token):
        abort(404)
    with _db_lock:
        conn = db_connect()
        row = conn.execute(
            "SELECT id, child_name, phone, pdf_filename, share_expires_at "
            "FROM special_orders WHERE share_token = ?",
            (token,),
        ).fetchone()
        if not row or not row["pdf_filename"]:
            conn.close()
            abort(404)
        # Siblings: orders for the same parent phone that have a PDF ready
        phone_key = _norm_order_phone(row["phone"] or "")
        siblings = []
        if phone_key:
            candidates = conn.execute(
                "SELECT id, child_name, phone FROM special_orders "
                "WHERE pdf_filename IS NOT NULL ORDER BY id"
            ).fetchall()
            siblings = [r for r in candidates if _norm_order_phone(r["phone"] or "") == phone_key]
        if not any(r["id"] == row["id"] for r in siblings):
            siblings.append(row)
        conn.close()

    if not _order_share_active(token, row["share_expires_at"]):
        return render_template("order_share.html", expired=True), 410

    exp = datetime.fromisoformat(row["share_expires_at"])
    kids = [{"id": r["id"], "name": (r["child_name"] or "طفلي")} for r in siblings]
    return render_template(
        "order_share.html",
        expired=False,
        token=token,
        kids=kids,
        main_id=row["id"],
        expires_label=exp.strftime("%Y/%m/%d"),
    )


@app.route("/so/<token>/pdf/<int:order_id>")
@limiter.limit("120 per hour")
def get_order_share_pdf(token: str, order_id: int):
    """Serve a PDF inline via share token — token order or a sibling (same phone)."""
    if not SHARE_ID_RE.match(token):
        abort(404)
    with _db_lock:
        conn = db_connect()
        token_row = conn.execute(
            "SELECT id, phone, pdf_filename, share_expires_at "
            "FROM special_orders WHERE share_token = ?",
            (token,),
        ).fetchone()
        if not token_row or not token_row["pdf_filename"]:
            conn.close()
            abort(404)
        target = conn.execute(
            "SELECT id, child_name, phone, pdf_filename FROM special_orders "
            "WHERE id = ? AND pdf_filename IS NOT NULL",
            (order_id,),
        ).fetchone()
        conn.close()
    if not target:
        abort(404)
    # Target must be the token's own order or a sibling (same parent phone)
    token_phone = _norm_order_phone(token_row["phone"] or "")
    target_phone = _norm_order_phone(target["phone"] or "")
    same_family = (target["id"] == token_row["id"]) or (
        token_phone and token_phone == target_phone
    )
    if not same_family:
        abort(404)
    if not _order_share_active(token, token_row["share_expires_at"]):
        abort(410)
    pdf_path = SPECIAL_ORDERS_DIR / str(target["id"]) / target["pdf_filename"]
    if not pdf_path.exists():
        abort(404)
    child = (target["child_name"] or "child").strip() or "child"
    # Inline so the parent can view the design directly in the browser
    return send_file(
        pdf_path,
        mimetype="application/pdf",
        download_name=f"{child}-coloring-book.pdf",
    )


if __name__ == "__main__":
    if not ACCOUNT_ID or not API_TOKEN:
        raise SystemExit("Set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN env vars.")
    app.run(host="127.0.0.1", port=5000, debug=False)
