import sqlite3, logging
from contextlib import contextmanager
from pathlib import Path

# Run the expensive log-trim only every N inserts to avoid a DELETE on every log line.
_log_write_counter = 0
_LOG_TRIM_EVERY = 50

logger = logging.getLogger(__name__)
DB_PATH = Path("data/firewalla_automator.db")

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                source_url TEXT NOT NULL UNIQUE,
                sync_interval_hours INTEGER NOT NULL DEFAULT 24,
                list_type TEXT NOT NULL DEFAULT 'domain',
                enabled INTEGER NOT NULL DEFAULT 1,
                sync_status TEXT NOT NULL DEFAULT 'pending',
                last_etag TEXT,
                last_modified TEXT,
                error_message TEXT,
                tags TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                slot_mode TEXT NOT NULL DEFAULT 'auto',
                allocated_slots INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS subscription_parts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                part_number INTEGER NOT NULL,
                firewalla_list_id TEXT,
                firewalla_list_name TEXT,
                entry_count INTEGER DEFAULT 0,
                last_synced_at TIMESTAMP,
                UNIQUE(subscription_id, part_number)
            );
            CREATE TABLE IF NOT EXISTS sync_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                message TEXT,
                entries_fetched INTEGER DEFAULT 0,
                entries_pushed INTEGER DEFAULT 0,
                parts_created INTEGER DEFAULT 0,
                parts_deleted INTEGER DEFAULT 0,
                duration_seconds REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS app_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT NOT NULL,
                logger TEXT,
                message TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_sync_logs_sub_id ON sync_logs(subscription_id);
            CREATE INDEX IF NOT EXISTS idx_parts_sub_id ON subscription_parts(subscription_id);
            CREATE INDEX IF NOT EXISTS idx_app_logs_level ON app_logs(level);
            CREATE INDEX IF NOT EXISTS idx_app_logs_created ON app_logs(created_at);
        """)
        # Live-migrate existing DBs — add columns if not yet present
        for col, defn in [
            ("slot_mode",       "TEXT NOT NULL DEFAULT 'auto'"),
            ("allocated_slots", "INTEGER"),
        ]:
            try:
                conn.execute(f"ALTER TABLE subscriptions ADD COLUMN {col} {defn}")
                logger.info(f"DB migrated: added subscriptions.{col}")
            except Exception:
                pass

        # Migrate old slot modes (auto/fixed) to the single rotate behaviour.
        # For old auto rows with no allocated_slots, use their current part count
        # so the list count stays stable across the update.
        conn.execute("""
            UPDATE subscriptions
            SET slot_mode = 'rotate',
                allocated_slots = COALESCE(
                    allocated_slots,
                    (SELECT COUNT(*) FROM subscription_parts
                     WHERE subscription_id = subscriptions.id),
                    1
                )
            WHERE slot_mode IN ('auto', 'fixed')
               OR allocated_slots IS NULL
        """)
        conn.commit()
    logger.info(f"Database initialised at {DB_PATH}")

@contextmanager
def get_db_connection():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def get_setting(key: str, default: str = "") -> str:
    try:
        with get_db_connection() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default
    except Exception:
        return default

def set_setting(key: str, value: str):
    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO app_settings(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
        """, (key, value))
        conn.commit()
