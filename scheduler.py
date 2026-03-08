"""
Sync Scheduler - asyncio loop that checks for due subscriptions.
"""
import os
import asyncio
import logging
from datetime import datetime, timedelta

from database import get_db_connection, get_setting

logger = logging.getLogger(__name__)
CHECK_INTERVAL_SECONDS = 60


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
        now = datetime.utcnow()
        with get_db_connection() as conn:
            subs = conn.execute("""
                SELECT s.id, s.name, s.sync_interval_hours,
                       MAX(l.created_at) as last_sync_time
                FROM subscriptions s
                LEFT JOIN sync_logs l ON s.id=l.subscription_id AND l.status IN ('success','skipped')
                WHERE s.enabled=1 AND s.sync_status != 'syncing'
                GROUP BY s.id
            """).fetchall()

        for sub in subs:
            sub = dict(sub)
            last = sub.get("last_sync_time")
            if last is None:
                due = True
            else:
                last_dt = datetime.fromisoformat(last.replace("Z", ""))
                due = now >= last_dt + timedelta(hours=sub["sync_interval_hours"])
            if due:
                logger.info(f"Scheduler: queuing sync for '{sub['name']}' (ID={sub['id']})")
                asyncio.create_task(self._run(sub["id"]))

    async def _run(self, sub_id: int):
        from firewalla_api import FirewallaAPI
        from list_manager import ListManager
        api_key = get_setting("FIREWALLA_API_KEY") or os.getenv("FIREWALLA_API_KEY", "")
        msp_domain = get_setting("FIREWALLA_MSP_DOMAIN") or os.getenv("FIREWALLA_MSP_DOMAIN", "")
        max_entries = int(get_setting("MAX_ENTRIES_PER_LIST") or os.getenv("MAX_ENTRIES_PER_LIST", "2000"))
        manager = ListManager(
            fw_api=FirewallaAPI(api_key=api_key, msp_domain=msp_domain),
            max_entries=max_entries
        )
        await manager.sync_subscription(sub_id)
