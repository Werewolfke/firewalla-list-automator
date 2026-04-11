"""
Sync Scheduler — asyncio loop that checks for due subscriptions every 60s.

Error backoff: after 3 consecutive sync failures the subscription is skipped
for 6 hours. A manual sync trigger (POST /api/subscriptions/{id}/sync) always
bypasses the backoff because it goes directly to a background task.
"""
import asyncio
import logging
from datetime import datetime, timedelta

from database import get_db_connection, get_setting

logger = logging.getLogger(__name__)
CHECK_INTERVAL_SECONDS = 60
ERROR_BACKOFF_HOURS = 6
CONSEC_ERROR_THRESHOLD = 3


class SyncScheduler:
    def __init__(self):
        self.is_running = False
        self._task: asyncio.Task = None

    async def start(self):
        self.is_running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Sync scheduler started (checking every 60s)")

    async def stop(self):
        self.is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        while self.is_running:
            try:
                await self._check_due()
            except Exception as e:
                logger.error(f"Scheduler loop error: {e}")
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

    async def _check_due(self):
        # Don't attempt syncs until the user has configured an API key.
        # The scheduler will start working automatically once one is saved.
        if not get_setting("FIREWALLA_API_KEY"):
            return

        now = datetime.utcnow()
        with get_db_connection() as conn:
            subs = conn.execute("""
                SELECT s.id, s.name, s.sync_interval_hours,
                       MAX(CASE WHEN l.status IN ('success','skipped')
                                THEN l.created_at END) AS last_success_time
                FROM subscriptions s
                LEFT JOIN sync_logs l ON s.id = l.subscription_id
                WHERE s.enabled = 1 AND s.sync_status != 'syncing'
                GROUP BY s.id
            """).fetchall()

        for sub in subs:
            sub = dict(sub)

            # ── Is it due? ────────────────────────────────────────────────
            last = sub.get("last_success_time")
            if last is None:
                due = True
            else:
                last_dt = datetime.fromisoformat(last.replace("Z", ""))
                due = now >= last_dt + timedelta(hours=sub["sync_interval_hours"])

            if not due:
                continue

            # ── Error backoff check ───────────────────────────────────────
            # If the last CONSEC_ERROR_THRESHOLD syncs were all errors,
            # wait ERROR_BACKOFF_HOURS before retrying automatically.
            with get_db_connection() as conn:
                recent = conn.execute(
                    "SELECT status, created_at FROM sync_logs "
                    "WHERE subscription_id=? ORDER BY id DESC LIMIT ?",
                    (sub["id"], CONSEC_ERROR_THRESHOLD)
                ).fetchall()

            consec_errors = 0
            for row in recent:
                if row["status"] == "error":
                    consec_errors += 1
                else:
                    break

            if consec_errors >= CONSEC_ERROR_THRESHOLD:
                last_err = datetime.fromisoformat(
                    recent[0]["created_at"].replace("Z", "")
                )
                retry_at = last_err + timedelta(hours=ERROR_BACKOFF_HOURS)
                if now < retry_at:
                    logger.debug(
                        f"'{sub['name']}': {consec_errors} consecutive errors, "
                        f"backoff until {retry_at.strftime('%H:%M UTC')} "
                        f"(use manual sync to override)"
                    )
                    continue

            logger.info(f"Scheduler: queuing sync for '{sub['name']}' (ID={sub['id']})")
            asyncio.create_task(self._run(sub["id"]))

    async def _run(self, sub_id: int):
        from firewalla_api import FirewallaAPI
        from list_manager import ListManager
        api_key    = get_setting("FIREWALLA_API_KEY")
        msp_domain = get_setting("FIREWALLA_MSP_DOMAIN")
        max_entries = int(get_setting("MAX_ENTRIES_PER_LIST") or "2000")
        manager = ListManager(
            fw_api=FirewallaAPI(api_key=api_key, msp_domain=msp_domain),
            max_entries=max_entries,
        )
        await manager.sync_subscription(sub_id)
