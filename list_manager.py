"""
List Manager - multi-URL fetch, dedupe, clean, slot-aware sync to Firewalla.

source_url field is newline-separated — multiple URLs are fetched in parallel,
combined, deduped, then distributed across slots.
"""

import re, time, random, logging, asyncio
from typing import Optional
import httpx
from database import get_db_connection
from firewalla_api import FirewallaAPI

logger = logging.getLogger(__name__)

DOMAIN_RE = re.compile(r'^(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$')
IP_V4_RE  = re.compile(r'^(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)(?:/\d{1,2})?$')
IP_V6_RE  = re.compile(
    r'^('
    r'([0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}|'           # 1:2:3:4:5:6:7:8
    r'([0-9a-fA-F]{1,4}:){1,7}:|'                          # 1::  through  1:2:3:4:5:6:7::
    r'([0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}|'         # 1::8  through  1:2:3:4:5:6::8
    r'([0-9a-fA-F]{1,4}:){1,5}(:[0-9a-fA-F]{1,4}){1,2}|'  # 1::7:8  through  1:2:3:4:5::7:8
    r'([0-9a-fA-F]{1,4}:){1,4}(:[0-9a-fA-F]{1,4}){1,3}|'
    r'([0-9a-fA-F]{1,4}:){1,3}(:[0-9a-fA-F]{1,4}){1,4}|'
    r'([0-9a-fA-F]{1,4}:){1,2}(:[0-9a-fA-F]{1,4}){1,5}|'
    r'[0-9a-fA-F]{1,4}:((:[0-9a-fA-F]{1,4}){1,6})|'
    r':((:[0-9a-fA-F]{1,4}){1,7}|:)|'                      # ::1  through  ::
    r'fe80:(:[0-9a-fA-F]{0,4}){0,4}%[0-9a-zA-Z]+|'         # link-local
    r'::(ffff(:0{1,4})?:)?((25[0-5]|(2[0-4]|1?[0-9])?[0-9])\.){3}'
    r'(25[0-5]|(2[0-4]|1?[0-9])?[0-9])|'                   # ::ffff:0:255.255.255.255
    r'([0-9a-fA-F]{1,4}:){1,4}:((25[0-5]|(2[0-4]|1?[0-9])?[0-9])\.){3}'
    r'(25[0-5]|(2[0-4]|1?[0-9])?[0-9])'                    # 2001:db8::255.255.255.255
    r')$'
)

def _valid(e):
    return bool(DOMAIN_RE.match(e) or IP_V4_RE.match(e) or IP_V6_RE.match(e))

def clean_list(raw: str, list_type: str = "domain") -> list:
    entries = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith(('#','!',';')): continue
        for sep in (' #','\t#'):
            if sep in line: line = line[:line.index(sep)].strip()
        parts = line.split()
        if len(parts)==2 and (parts[0] in ('0.0.0.0','127.0.0.1','::1') or IP_V4_RE.match(parts[0])):
            candidate = parts[1].lower()
            if candidate in ('localhost','broadcasthost'): continue
            line = candidate
        elif len(parts)>1: line = parts[0].lower()
        else: line = line.lower()
        for p in ('http://','https://','www.'):
            if line.startswith(p): line = line[len(p):]
        line = line.split('/')[0].strip()
        if line and _valid(line): entries.add(line)
    return sorted(entries)

def chunk_list(entries, size):
    return [entries[i:i+size] for i in range(0, len(entries), size)]

def parse_urls(source_url: str) -> list:
    """Split newline/comma-separated URLs, strip whitespace, drop empties."""
    urls = []
    for u in re.split(r'[\n\r,]+', source_url):
        u = u.strip()
        if u and u.startswith('http'): urls.append(u)
    return urls

async def fetch_one(url: str) -> tuple:
    """Fetch a single URL. Returns (url, text, error)."""
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            r = await client.get(url)
        r.raise_for_status()
        return url, r.text, None
    except Exception as e:
        return url, None, str(e)

async def preview_urls(source_url: str, max_entries: int = 2000) -> dict:
    """Fetch all URLs, combine, dedupe, return analysis. Capped at 90s total."""
    urls = parse_urls(source_url)
    if not urls:
        return {"ok": False, "error": "No valid URLs found"}
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*[fetch_one(u) for u in urls]),
            timeout=90.0,
        )
    except asyncio.TimeoutError:
        return {"ok": False, "error": "Preview timed out after 90s — URLs may be too slow or unreachable"}
    all_entries = set()
    errors = []
    url_stats = []
    for url, text, err in results:
        if err:
            errors.append(f"{url}: {err}")
            url_stats.append({"url": url, "entries": 0, "error": err})
        else:
            entries = clean_list(text)
            before = len(all_entries)
            all_entries.update(entries)
            added = len(all_entries) - before
            url_stats.append({"url": url, "entries": len(entries), "added_unique": added})
    combined = sorted(all_entries)
    min_slots = max(1, -(-len(combined) // max_entries))
    return {
        "ok": True,
        "total_entries": len(combined),
        "min_slots_needed": min_slots,
        "url_count": len(urls),
        "url_stats": url_stats,
        "errors": errors,
        "sample": combined[:5],
    }

class ListManager:
    def __init__(self, fw_api: FirewallaAPI, max_entries: int = 2000):
        self.fw_api = fw_api
        self.max_entries = max_entries

    async def fetch_all_urls(self, source_url: str, etag: str, last_modified: str):
        """
        Fetch all URLs in source_url (newline-separated).
        For single-URL subscriptions, honours ETag/Last-Modified caching.
        For multi-URL, always fetches all (no reliable way to cache across sources).
        Returns (combined_text, new_etag, new_last_modified, changed).
        """
        urls = parse_urls(source_url)
        if not urls:
            raise RuntimeError("No valid URLs in source_url")

        if len(urls) == 1:
            # Single URL — use ETag caching
            url = urls[0]
            headers = {}
            if etag: headers["If-None-Match"] = etag
            if last_modified: headers["If-Modified-Since"] = last_modified
            try:
                async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                    r = await client.get(url, headers=headers)
                if r.status_code == 304:
                    logger.info(f"Source unchanged (304): {url}")
                    return None, etag, last_modified, False
                r.raise_for_status()
                return r.text, r.headers.get("ETag"), r.headers.get("Last-Modified"), True
            except httpx.HTTPStatusError as e:
                raise RuntimeError(f"HTTP {e.response.status_code} fetching {url}")
            except httpx.RequestError as e:
                raise RuntimeError(f"Network error: {e}")
        else:
            # Multi-URL: fetch all in parallel, combine
            logger.info(f"Fetching {len(urls)} URLs in parallel…")
            results = await asyncio.gather(*[fetch_one(u) for u in urls])
            combined = []
            for url, text, err in results:
                if err:
                    logger.warning(f"Failed to fetch {url}: {err}")
                elif text:
                    combined.append(text)
                    logger.info(f"  Fetched {url} — {len(text)} chars")
            if not combined:
                raise RuntimeError("All URLs failed to fetch")
            return "\n".join(combined), None, None, True

    async def reconcile_parts(self, sub_id: int, sub_name: str, expected_slots: int) -> dict:
        logger.info(f"Reconciling {expected_slots} slots for '{sub_name}'…")
        live = await self.fw_api.get_all_lists()
        live_by_id   = {l["id"]: l for l in live if "id" in l}
        live_by_name = {l["name"]: l["id"] for l in live if "name" in l}
        with get_db_connection() as conn:
            db_parts = [dict(p) for p in conn.execute(
                "SELECT * FROM subscription_parts WHERE subscription_id=? ORDER BY part_number", (sub_id,)
            ).fetchall()]
        confirmed = {}
        for p in db_parts:
            pnum = p["part_number"]
            sid  = p.get("firewalla_list_id")
            if sid and sid in live_by_id:
                confirmed[pnum] = sid
            else:
                ename = self._pname(sub_name, pnum, expected_slots)
                if ename in live_by_name:
                    aid = live_by_name[ename]
                    logger.warning(f"  Slot {pnum}: adopting '{ename}' → {aid}")
                    confirmed[pnum] = aid
                    self._upsert_part(sub_id, pnum, aid, ename, p.get("entry_count",0))
        tracked = set(confirmed.values())
        for pnum in range(1, expected_slots+1):
            if pnum in confirmed: continue
            name = self._pname(sub_name, pnum, expected_slots)
            if name in live_by_name and live_by_name[name] not in tracked:
                aid = live_by_name[name]
                logger.warning(f"  Slot {pnum}: untracked list '{name}' → adopting {aid}")
                confirmed[pnum] = aid
                self._upsert_part(sub_id, pnum, aid, name, 0)
        logger.info(f"Reconcile: {len(confirmed)}/{expected_slots} confirmed")
        return confirmed

    async def sync_subscription(self, sub_id: int):
        start = time.time()
        with get_db_connection() as conn:
            sub = conn.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
            if not sub: logger.error(f"Sub {sub_id} not found"); return
        sub = dict(sub)
        alloc = sub.get("allocated_slots")
        urls  = parse_urls(sub["source_url"])
        logger.info(f"━━ Sync: '{sub['name']}' | {len(urls)} URL(s) | lists={alloc} ━━")
        self._update_status(sub_id, "syncing", None)
        try:
            content, new_etag, new_lm, changed = await self.fetch_all_urls(
                sub["source_url"], sub.get("last_etag"), sub.get("last_modified")
            )
            with get_db_connection() as conn:
                existing = [dict(p) for p in conn.execute(
                    "SELECT * FROM subscription_parts WHERE subscription_id=? ORDER BY part_number", (sub_id,)
                ).fetchall()]
            if not changed and existing:
                logger.info(f"No changes for '{sub['name']}' — skipping")
                self._update_status(sub_id, "ok", None)
                self._log_sync(sub_id, "skipped", "No changes (ETag match)", 0, 0, 0, 0, time.time()-start)
                return

            # Clean + dedupe across all URLs
            all_entries = clean_list(content or "", sub["list_type"])
            total = len(all_entries)
            min_slots = max(1, -(-total // self.max_entries))

            # Target number of lists: user-specified or auto-calculated minimum
            target = alloc if alloc and alloc >= 1 else min_slots
            logger.info(f"Cleaned: {total} unique entries — using {target} list(s) "
                        f"(min needed: {min_slots})")

            # Always shuffle before distributing
            shuffled = list(all_entries)
            random.shuffle(shuffled)

            coverage_pct = 100
            if total <= target * self.max_entries:
                # All entries fit — distribute evenly so each list is roughly equal size
                per_slot = max(1, -(-total // target))  # ceiling division
                chunks = chunk_list(shuffled, per_slot)
                chunks = chunks + [[] for _ in range(max(0, target - len(chunks)))]
                logger.info(f"Distributing evenly: ~{per_slot} entries per list")
            else:
                # List overflows the allocated capacity — randomly select what fits
                # and rotate coverage on every sync
                capacity = target * self.max_entries
                selected = shuffled[:capacity]
                chunks = chunk_list(selected, self.max_entries)
                chunks = chunks + [[] for _ in range(max(0, target - len(chunks)))]
                coverage_pct = round(capacity / total * 100, 1)
                logger.warning(
                    f"Overflow: {total} entries > {capacity} capacity "
                    f"({coverage_pct}% covered per sync — consider adding more lists)"
                )

            confirmed = await self.reconcile_parts(sub_id, sub["name"], target)
            created = patched = 0
            pushed = 0

            for i, chunk in enumerate(chunks):
                pnum  = i + 1
                pname = self._pname(sub["name"], pnum, target)

                # Never try to create an empty list — Firewalla rejects them.
                # For fixed/rotate modes we may have trailing empty padding slots
                # that haven't been created yet; simply skip them.
                if not chunk and pnum not in confirmed:
                    logger.info(f"Slot {pnum}: empty and not yet created — skipping")
                    continue

                notes = sub.get("notes") or ""
                if pnum in confirmed:
                    fw_id = confirmed[pnum]
                    ok = await self.fw_api.update_list(fw_id, pname, chunk, sub["list_type"], notes=notes)
                    if not ok: raise RuntimeError(f"PATCH failed slot {pnum} ({fw_id})")
                    self._upsert_part(sub_id, pnum, fw_id, pname, len(chunk))
                    pushed += len(chunk); patched += 1
                else:
                    fw_id = await self.fw_api.create_list(pname, chunk, sub["list_type"], notes=notes)
                    if not fw_id: raise RuntimeError(f"CREATE failed slot {pnum}")
                    self._upsert_part(sub_id, pnum, fw_id, pname, len(chunk))
                    pushed += len(chunk); created += 1

            # Delete orphans (lists beyond current target count)
            deleted = 0
            with get_db_connection() as conn:
                all_parts = [dict(p) for p in conn.execute(
                    "SELECT * FROM subscription_parts WHERE subscription_id=?", (sub_id,)
                ).fetchall()]
            for p in all_parts:
                if p["part_number"] > target:
                    if p.get("firewalla_list_id"):
                        ok = await self.fw_api.delete_list(p["firewalla_list_id"])
                        if ok: deleted += 1
                    with get_db_connection() as conn:
                        conn.execute("DELETE FROM subscription_parts WHERE id=?", (p["id"],))
                        conn.commit()

            with get_db_connection() as conn:
                conn.execute("""UPDATE subscriptions SET sync_status='ok',error_message=NULL,
                    last_etag=?,last_modified=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (new_etag, new_lm, sub_id))
                conn.commit()

            dur = time.time()-start
            src_info = f"{len(urls)} source(s)" if len(urls) > 1 else "1 source"
            cov_info = f" ~{coverage_pct}% coverage" if coverage_pct < 100 else ""
            msg = (f"{src_info} → {total} unique entries | {target} list(s){cov_info} | "
                   f"{created}✚ {patched}↺ {deleted}✕")
            self._log_sync(sub_id, "success", msg, total, pushed, created, deleted, dur)
            logger.info(f"━━ Done: '{sub['name']}' {msg} in {dur:.1f}s ━━")

        except Exception as e:
            err = str(e)
            logger.error(f"Sync FAILED '{sub['name']}': {err}")
            self._update_status(sub_id,"error",err)
            self._log_sync(sub_id,"error",err,0,0,0,0,time.time()-start)

    @staticmethod
    def _pname(name, pnum, total):
        return name if total==1 else f"{name}_Part{pnum}"

    def _update_status(self, sub_id, status, error):
        with get_db_connection() as conn:
            conn.execute("UPDATE subscriptions SET sync_status=?,error_message=? WHERE id=?",
                         (status,error,sub_id))
            conn.commit()

    def _upsert_part(self, sub_id, pnum, fw_id, name, count):
        with get_db_connection() as conn:
            conn.execute("""
                INSERT INTO subscription_parts
                    (subscription_id,part_number,firewalla_list_id,firewalla_list_name,entry_count,last_synced_at)
                VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(subscription_id,part_number) DO UPDATE SET
                    firewalla_list_id=excluded.firewalla_list_id,
                    firewalla_list_name=excluded.firewalla_list_name,
                    entry_count=excluded.entry_count,
                    last_synced_at=excluded.last_synced_at
            """, (sub_id,pnum,fw_id,name,count))
            conn.commit()

    def _log_sync(self, sub_id, status, msg, fetched, pushed, created, deleted, dur):
        with get_db_connection() as conn:
            conn.execute("""INSERT INTO sync_logs
                (subscription_id,status,message,entries_fetched,entries_pushed,
                 parts_created,parts_deleted,duration_seconds)
                VALUES(?,?,?,?,?,?,?,?)""",
                (sub_id,status,msg,fetched,pushed,created,deleted,round(dur,2)))
            conn.commit()
