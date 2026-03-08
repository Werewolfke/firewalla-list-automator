import os, asyncio, logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

from database import init_db, get_db_connection, get_setting, set_setting
from firewalla_api import FirewallaAPI
from scheduler import SyncScheduler
from list_manager import ListManager, preview_urls
from models import SubscriptionCreate, SubscriptionUpdate

load_dotenv()

# ── DB log handler ─────────────────────────────────────────────────────────────
class DBLogHandler(logging.Handler):
    def emit(self, record):
        try:
            with get_db_connection() as conn:
                conn.execute("INSERT INTO app_logs(level,logger,message) VALUES(?,?,?)",
                             (record.levelname, record.name, self.format(record)[:2000]))
                conn.execute("DELETE FROM app_logs WHERE id NOT IN (SELECT id FROM app_logs ORDER BY id DESC LIMIT 500)")
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
        api_key=get_setting("FIREWALLA_API_KEY") or os.getenv("FIREWALLA_API_KEY", ""),
        msp_domain=get_setting("FIREWALLA_MSP_DOMAIN") or os.getenv("FIREWALLA_MSP_DOMAIN", "")
    )

@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler
    logger.info("Starting Firewalla Feed Automator…")
    init_db()
    for k in ("FIREWALLA_API_KEY", "FIREWALLA_MSP_DOMAIN"):
        if not get_setting(k) and os.getenv(k):
            set_setting(k, os.getenv(k))
    scheduler = SyncScheduler()
    await scheduler.start()
    yield
    await scheduler.stop()

app = FastAPI(title="Firewalla Feed Automator", version="2.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ── Pages ──────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})

# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    s = await _fw().check_health()
    logger.info(f"Health check result: {s}")
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat(),
            "firewalla_api": s, "scheduler_running": scheduler.is_running if scheduler else False}

# ── Preview ────────────────────────────────────────────────────────────────────

@app.get("/api/preview")
async def preview(url: str):
    """
    GET /api/preview?url=<newline-encoded URLs>
    Fetches all URLs, combines, dedupes, returns analysis. Nothing is saved.
    """
    max_e = int(get_setting("MAX_ENTRIES_PER_LIST") or os.getenv("MAX_ENTRIES_PER_LIST", "2000"))
    return await preview_urls(url, max_e)

# ── Settings ───────────────────────────────────────────────────────────────────

from pydantic import BaseModel as PM

class SettingsUpdate(PM):
    firewalla_msp_domain: str
    firewalla_api_key: str
    max_entries_per_list: Optional[int] = 2000

@app.get("/api/settings")
async def get_settings():
    key = get_setting("FIREWALLA_API_KEY") or os.getenv("FIREWALLA_API_KEY", "")
    return {
        "firewalla_msp_domain": get_setting("FIREWALLA_MSP_DOMAIN") or os.getenv("FIREWALLA_MSP_DOMAIN", ""),
        "firewalla_api_key_set": bool(key),
        "firewalla_api_key_masked": (key[:4] + "••••" + key[-4:]) if len(key) >= 8 else "••••••••",
        "max_entries_per_list": int(get_setting("MAX_ENTRIES_PER_LIST") or os.getenv("MAX_ENTRIES_PER_LIST", "2000")),
    }

@app.put("/api/settings")
async def save_settings(body: SettingsUpdate):
    if body.firewalla_msp_domain:
        set_setting("FIREWALLA_MSP_DOMAIN", body.firewalla_msp_domain.strip())
    if body.firewalla_api_key and body.firewalla_api_key != "••••••••":
        set_setting("FIREWALLA_API_KEY", body.firewalla_api_key.strip())
    if body.max_entries_per_list:
        set_setting("MAX_ENTRIES_PER_LIST", str(body.max_entries_per_list))
    logger.info("Settings updated via UI")
    return {"message": "Saved", "connection_test": await _fw().check_health()}

# ── App logs ───────────────────────────────────────────────────────────────────

@app.get("/api/logs")
async def get_logs(level: str = "INFO", limit: int = 100):
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
async def clear_logs():
    with get_db_connection() as conn:
        conn.execute("DELETE FROM app_logs")
        conn.commit()
    return {"message": "Cleared"}

# ── Subscriptions ──────────────────────────────────────────────────────────────

@app.get("/api/subscriptions")
async def list_subs():
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
async def get_sub(sid: int):
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
async def create_sub(sub: SubscriptionCreate, bg: BackgroundTasks):
    with get_db_connection() as conn:
        try:
            cur = conn.execute("""
                INSERT INTO subscriptions
                    (name, source_url, sync_interval_hours, list_type, enabled,
                     tags, notes, slot_mode, allocated_slots)
                VALUES (?,?,?,?,1,?,?,?,?)
            """, (sub.name, str(sub.source_url), sub.sync_interval_hours, sub.list_type,
                  sub.tags or "", sub.notes or "", sub.slot_mode or "auto", sub.allocated_slots))
            sid = cur.lastrowid
            conn.commit()
        except Exception as e:
            raise HTTPException(400, str(e))
    logger.info(f"Created subscription '{sub.name}' (ID={sid})")
    bg.add_task(_sync_task, sid)
    return {"id": sid, "message": "Created, initial sync queued"}

@app.put("/api/subscriptions/{sid}")
async def update_sub(sid: int, sub: SubscriptionUpdate):
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
                slot_mode          = COALESCE(?, slot_mode),
                allocated_slots    = COALESCE(?, allocated_slots),
                updated_at         = CURRENT_TIMESTAMP
            WHERE id=?
        """, (sub.name, sub.source_url, sub.sync_interval_hours, sub.enabled,
              sub.tags, sub.notes, sub.slot_mode, sub.allocated_slots, sid))
        conn.commit()
    return {"message": "Updated"}

@app.delete("/api/subscriptions/{sid}")
async def delete_sub(sid: int):
    with get_db_connection() as conn:
        sub   = conn.execute("SELECT * FROM subscriptions WHERE id=?", (sid,)).fetchone()
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
async def trigger_sync(sid: int, bg: BackgroundTasks):
    with get_db_connection() as conn:
        if not conn.execute("SELECT id FROM subscriptions WHERE id=?", (sid,)).fetchone():
            raise HTTPException(404, "Not found")
        conn.execute("UPDATE subscriptions SET sync_status='queued' WHERE id=?", (sid,))
        conn.commit()
    bg.add_task(_sync_task, sid)
    return {"message": "Sync queued"}

@app.get("/api/subscriptions/{sid}/logs")
async def sub_logs(sid: int, limit: int = 50):
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
