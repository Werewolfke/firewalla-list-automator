import os, asyncio, logging, hmac, hashlib
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
from pydantic import BaseModel

from database import init_db, get_db_connection, get_setting, set_setting, _LOG_TRIM_EVERY
from firewalla_api import FirewallaAPI, close_http_client
from scheduler import SyncScheduler
from list_manager import ListManager, preview_urls
from models import SubscriptionCreate, SubscriptionUpdate

load_dotenv()

def _read_version() -> str:
    try:
        return (Path(__file__).parent / "VERSION").read_text().strip()
    except Exception:
        return "unknown"

APP_VERSION = _read_version()
COOKIE_NAME = "fwa_session"

# ── Auth helpers ───────────────────────────────────────────────────────────────

def _expected_token() -> str:
    """Derive a stable session token from SECRET_KEY. Constant per install."""
    secret = os.getenv("SECRET_KEY", "no-secret-set")
    return hmac.new(secret.encode(), b"fwa-auth-v1", hashlib.sha256).hexdigest()

def _is_authed(request: Request) -> bool:
    return hmac.compare_digest(
        request.cookies.get(COOKIE_NAME, ""),
        _expected_token()
    )

def _is_configured() -> bool:
    """Returns True once the user has completed first-run setup (password set)."""
    return bool(get_setting("APP_PASSWORD"))

async def require_auth(request: Request):
    """FastAPI dependency — raises 401 for unauthenticated API calls."""
    if not _is_authed(request):
        raise HTTPException(status_code=401, detail="Not authenticated")

# ── DB log handler ─────────────────────────────────────────────────────────────

class DBLogHandler(logging.Handler):
    _counter = 0

    def emit(self, record):
        try:
            DBLogHandler._counter += 1
            trim = (DBLogHandler._counter % _LOG_TRIM_EVERY == 0)
            with get_db_connection() as conn:
                conn.execute(
                    "INSERT INTO app_logs(level,logger,message) VALUES(?,?,?)",
                    (record.levelname, record.name, self.format(record)[:2000])
                )
                if trim:
                    conn.execute(
                        "DELETE FROM app_logs WHERE id NOT IN "
                        "(SELECT id FROM app_logs ORDER BY id DESC LIMIT 500)"
                    )
                conn.commit()
        except Exception:
            pass

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
_dbh = DBLogHandler()
_dbh.setLevel(logging.INFO)
_dbh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logging.getLogger().addHandler(_dbh)
logger = logging.getLogger(__name__)

scheduler: SyncScheduler = None

def _fw():
    return FirewallaAPI(
        api_key=get_setting("FIREWALLA_API_KEY"),
        msp_domain=get_setting("FIREWALLA_MSP_DOMAIN"),
    )

@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler
    logger.info("Starting Firewalla Feed Automator…")
    init_db()
    scheduler = SyncScheduler()
    await scheduler.start()
    yield
    await scheduler.stop()
    await close_http_client()

app = FastAPI(title="Firewalla Feed Automator", version=APP_VERSION, lifespan=lifespan)
templates = Jinja2Templates(directory="templates")

# ── Setup (first-run) ─────────────────────────────────────────────────────────

@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    if _is_configured():
        return RedirectResponse("/")
    return templates.TemplateResponse("setup.html", {"request": request, "error": None})

@app.post("/setup", response_class=HTMLResponse)
async def do_setup(
    request: Request,
    msp_domain: str = Form(...),
    api_key: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
):
    if _is_configured():
        return RedirectResponse("/", status_code=303)

    errors = []
    msp_domain = msp_domain.strip()
    for prefix in ("https://", "http://"):
        if msp_domain.startswith(prefix):
            msp_domain = msp_domain[len(prefix):]
            break
    msp_domain = msp_domain.rstrip("/")
    api_key = api_key.strip()

    if not msp_domain:
        errors.append("MSP domain is required.")
    if not api_key:
        errors.append("API key is required.")
    if not password:
        errors.append("Password is required.")
    if password != confirm_password:
        errors.append("Passwords do not match.")
    if len(password) < 8:
        errors.append("Password must be at least 8 characters.")

    if errors:
        return templates.TemplateResponse(
            "setup.html", {"request": request, "error": " ".join(errors)}
        )

    set_setting("FIREWALLA_MSP_DOMAIN", msp_domain)
    set_setting("FIREWALLA_API_KEY", api_key)
    set_setting("APP_PASSWORD", password)
    logger.info("First-run setup completed via web UI")

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(COOKIE_NAME, _expected_token(), httponly=True, samesite="strict")
    return response

# ── Login / Logout ────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if not _is_configured():
        return RedirectResponse("/setup")
    if _is_authed(request):
        return RedirectResponse("/")
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

@app.post("/login", response_class=HTMLResponse)
async def do_login(request: Request, password: str = Form(...)):
    if not _is_configured():
        return RedirectResponse("/setup", status_code=303)
    stored = get_setting("APP_PASSWORD")
    if password == stored:
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE_NAME, _expected_token(), httponly=True, samesite="strict")
        return response
    logger.warning("Failed login attempt")
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Invalid password."}
    )

@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response

# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not _is_configured():
        return RedirectResponse("/setup")
    if not _is_authed(request):
        return RedirectResponse("/login")
    return templates.TemplateResponse("dashboard.html", {"request": request})

# ── Health (public — used by external monitors) ───────────────────────────────

@app.get("/api/health")
async def health():
    s = await _fw().check_health()
    return {
        "status": "ok",
        "version": APP_VERSION,
        "timestamp": datetime.utcnow().isoformat(),
        "firewalla_api": s,
        "scheduler_running": scheduler.is_running if scheduler else False,
    }

# ── Version ───────────────────────────────────────────────────────────────────

@app.get("/api/version")
async def get_version():
    return {"version": APP_VERSION}

# ── Preview ────────────────────────────────────────────────────────────────────

@app.get("/api/preview")
async def preview(url: str, _: None = Depends(require_auth)):
    max_e = int(get_setting("MAX_ENTRIES_PER_LIST") or os.getenv("MAX_ENTRIES_PER_LIST", "2000"))
    return await preview_urls(url, max_e)

# ── Settings ───────────────────────────────────────────────────────────────────

class SettingsUpdate(BaseModel):
    firewalla_msp_domain: str
    firewalla_api_key: str
    max_entries_per_list: Optional[int] = 2000
    current_password: Optional[str] = None
    new_password: Optional[str] = None

@app.get("/api/settings")
async def get_settings(_: None = Depends(require_auth)):
    key = get_setting("FIREWALLA_API_KEY")
    return {
        "firewalla_msp_domain": get_setting("FIREWALLA_MSP_DOMAIN"),
        "firewalla_api_key_set": bool(key),
        "firewalla_api_key_masked": (key[:4] + "••••" + key[-4:]) if len(key) >= 8 else "••••••••",
        "max_entries_per_list": int(get_setting("MAX_ENTRIES_PER_LIST") or os.getenv("MAX_ENTRIES_PER_LIST", "2000")),
    }

@app.put("/api/settings")
async def save_settings(body: SettingsUpdate, _: None = Depends(require_auth)):
    if body.firewalla_msp_domain:
        set_setting("FIREWALLA_MSP_DOMAIN", body.firewalla_msp_domain.strip())
    if body.firewalla_api_key and body.firewalla_api_key != "••••••••":
        set_setting("FIREWALLA_API_KEY", body.firewalla_api_key.strip())
    if body.max_entries_per_list:
        set_setting("MAX_ENTRIES_PER_LIST", str(body.max_entries_per_list))

    # Optional password change
    if body.new_password:
        stored = get_setting("APP_PASSWORD")
        if body.current_password != stored:
            raise HTTPException(400, "Current password is incorrect")
        if len(body.new_password) < 8:
            raise HTTPException(400, "New password must be at least 8 characters")
        set_setting("APP_PASSWORD", body.new_password)
        logger.info("Password changed via Settings")

    logger.info("Settings updated via UI")
    return {"message": "Saved", "connection_test": await _fw().check_health()}

# ── App logs ───────────────────────────────────────────────────────────────────

@app.get("/api/logs")
async def get_logs(level: str = "INFO", limit: int = 100, _: None = Depends(require_auth)):
    lvls = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
    min_l = lvls.get(level.upper(), 1)
    keep = [k for k, v in lvls.items() if v >= min_l]
    ph = ",".join("?" * len(keep))
    with get_db_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM app_logs WHERE level IN ({ph}) ORDER BY id DESC LIMIT ?",
            (*keep, limit)
        ).fetchall()
    return [dict(r) for r in rows]

@app.delete("/api/logs")
async def clear_logs(_: None = Depends(require_auth)):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM app_logs")
        conn.commit()
    return {"message": "Cleared"}

# ── Subscriptions ──────────────────────────────────────────────────────────────

@app.get("/api/subscriptions")
async def list_subs(_: None = Depends(require_auth)):
    with get_db_connection() as conn:
        rows = conn.execute("""
            SELECT s.*,
                COUNT(DISTINCT p.id)  AS part_count,
                MAX(p.last_synced_at) AS last_synced_at,
                SUM(p.entry_count)    AS total_entries
            FROM subscriptions s
            LEFT JOIN subscription_parts p ON s.id = p.subscription_id
            GROUP BY s.id ORDER BY s.created_at DESC
        """).fetchall()
    return [dict(r) for r in rows]

@app.get("/api/subscriptions/{sid}")
async def get_sub(sid: int, _: None = Depends(require_auth)):
    with get_db_connection() as conn:
        sub = conn.execute("SELECT * FROM subscriptions WHERE id=?", (sid,)).fetchone()
        if not sub:
            raise HTTPException(404, "Not found")
        parts = conn.execute(
            "SELECT * FROM subscription_parts WHERE subscription_id=? ORDER BY part_number", (sid,)
        ).fetchall()
        r = dict(sub)
        r["parts"] = [dict(p) for p in parts]
    return r

@app.post("/api/subscriptions")
async def create_sub(sub: SubscriptionCreate, bg: BackgroundTasks, _: None = Depends(require_auth)):
    with get_db_connection() as conn:
        try:
            cur = conn.execute("""
                INSERT INTO subscriptions
                    (name, source_url, sync_interval_hours, list_type, enabled,
                     tags, notes, slot_mode, allocated_slots)
                VALUES (?,?,?,?,1,?,?,?,?)
            """, (sub.name, str(sub.source_url), sub.sync_interval_hours, sub.list_type,
                  sub.tags or "", sub.notes or "", "rotate", sub.allocated_slots))
            sid = cur.lastrowid
            conn.commit()
        except Exception as e:
            raise HTTPException(400, str(e))
    logger.info(f"Created subscription '{sub.name}' (ID={sid})")
    bg.add_task(_sync_task, sid)
    return {"id": sid, "message": "Created, initial sync queued"}

@app.put("/api/subscriptions/{sid}")
async def update_sub(sid: int, sub: SubscriptionUpdate, _: None = Depends(require_auth)):
    with get_db_connection() as conn:
        if not conn.execute("SELECT id FROM subscriptions WHERE id=?", (sid,)).fetchone():
            raise HTTPException(404, "Not found")
        conn.execute("""
            UPDATE subscriptions SET
                name               = COALESCE(?, name),
                source_url         = COALESCE(?, source_url),
                sync_interval_hours= COALESCE(?, sync_interval_hours),
                enabled            = COALESCE(?, enabled),
                tags               = COALESCE(?, tags),
                notes              = COALESCE(?, notes),
                allocated_slots    = COALESCE(?, allocated_slots),
                updated_at         = CURRENT_TIMESTAMP
            WHERE id=?
        """, (sub.name, sub.source_url, sub.sync_interval_hours, sub.enabled,
              sub.tags, sub.notes, sub.allocated_slots, sid))
        conn.commit()
    return {"message": "Updated"}

@app.delete("/api/subscriptions/{sid}")
async def delete_sub(sid: int, _: None = Depends(require_auth)):
    with get_db_connection() as conn:
        sub = conn.execute("SELECT * FROM subscriptions WHERE id=?", (sid,)).fetchone()
        if not sub:
            raise HTTPException(404, "Not found")
        parts = conn.execute(
            "SELECT * FROM subscription_parts WHERE subscription_id=?", (sid,)
        ).fetchall()
    deleted = 0
    for p in parts:
        if p["firewalla_list_id"]:
            if await _fw().delete_list(p["firewalla_list_id"]):
                deleted += 1
    with get_db_connection() as conn:
        conn.execute("DELETE FROM subscriptions WHERE id=?", (sid,))
        conn.commit()
    logger.info(f"Deleted '{dict(sub)['name']}', removed {deleted} FW list(s)")
    return {"message": f"Deleted. {deleted} Firewalla list(s) removed."}

@app.post("/api/subscriptions/{sid}/sync")
async def trigger_sync(sid: int, bg: BackgroundTasks, _: None = Depends(require_auth)):
    with get_db_connection() as conn:
        if not conn.execute("SELECT id FROM subscriptions WHERE id=?", (sid,)).fetchone():
            raise HTTPException(404, "Not found")
        conn.execute("UPDATE subscriptions SET sync_status='queued' WHERE id=?", (sid,))
        conn.commit()
    bg.add_task(_sync_task, sid)
    return {"message": "Sync queued"}

@app.get("/api/subscriptions/{sid}/logs")
async def sub_logs(sid: int, limit: int = 50, _: None = Depends(require_auth)):
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM sync_logs WHERE subscription_id=? ORDER BY created_at DESC LIMIT ?",
            (sid, limit)
        ).fetchall()
    return [dict(r) for r in rows]

async def _sync_task(sid: int):
    max_e = int(get_setting("MAX_ENTRIES_PER_LIST") or os.getenv("MAX_ENTRIES_PER_LIST", "2000"))
    await ListManager(fw_api=_fw(), max_entries=max_e).sync_subscription(sid)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=os.getenv("HOST", "0.0.0.0"),
                port=int(os.getenv("PORT", "8080")), reload=False)
