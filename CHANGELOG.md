# Changelog

All notable changes to Firewalla Feed Automator are documented here.

---

## [1.2.1] — 2026-04-11

### Fixed
- **`IndentationError` in `firewalla_api.py`** (`_request` method): when the shared
  `httpx` client was introduced in 1.2.0, the `async with` block body was not dedented
  after removing the context manager. This caused a Python `IndentationError` at startup,
  preventing uvicorn from loading the app entirely — the root cause of the page being
  inaccessible after upgrading to 1.2.0.
- **`lstrip` prefix stripping bug in `app.py`** (`do_setup`): `str.lstrip("https://")`
  strips individual characters, not the full prefix string, which could silently corrupt
  MSP domain names starting with the letters `h`, `t`, `p`, `s`, `:`, or `/`. Replaced
  with an explicit `startswith` + slice.
- **`asyncio.get_event_loop()` deprecation** (`firewalla_api.py`): replaced with
  `asyncio.get_running_loop()` for Python 3.10+ compatibility.

---

## [1.2.0] — 2026-04-11

### Changed — slot system completely simplified
The three-mode slot system (auto / fixed / rotate) has been replaced with a single, unified
behaviour. Users now only answer one question: **how many Firewalla lists do you want?**

**How it works now:**
- Entries are always shuffled randomly before being distributed.
- If the list fits within the allocated capacity, entries are spread evenly across all lists.
- If the list grows beyond capacity, a random subset is used each sync — coverage rotates
  automatically over time without any user action.
- Lists beyond the current count are always cleaned up (no more "fixed" phantom empty lists).

**Why:** Auto mode created new lists on every growth event, which broke Firewalla policy
group assignments. Fixed mode silently dropped entries when lists grew. The new single
behaviour keeps list names stable, distributes load evenly, and degrades gracefully when a
source grows larger than expected.

### Removed
- `slot_mode` field (`auto`, `fixed`, `rotate`) — removed from API, models, and UI entirely.
- The mode-selector radio buttons and all associated JS (`onModeChange`, `updateRotateCalc`,
  `onEditModeChange`) are gone from the dashboard.

### Added
- **Growth warning** always shown in the Add and Edit modals, explaining that lists can grow
  and users should allocate buffer slots.
- **Slot calculator** now shows a single live feedback widget: headroom if fits, or
  percentage coverage per sync if it overflows.
- **Quick-suggest buttons** after preview: Min / +1 buffer / +50% / 2× instead of the
  previous fixed/rotate-specific suggestions.

### Migration
Existing databases are automatically migrated on first startup: all `auto` and `fixed`
subscriptions are converted to the new behaviour. For `auto` subscriptions with no
`allocated_slots`, the current part count is used so nothing changes on the Firewalla side.

---

## [1.1.0] — 2026-04-11

### Added
- **First-run setup wizard** (`/setup`): on the very first visit after install the web UI
  asks for MSP domain, API key, and a login password. No credentials are entered during
  `install.sh` anymore — everything is configured through the browser.
- **Password-only login page** (`/login`): all pages and API endpoints are now protected
  by a session cookie. The cookie is signed with `SECRET_KEY` from `.env` (which
  `install.sh` generates automatically).
- **Logout** (`/logout`): sign-out button in the dashboard header clears the session cookie.
- **Password change in Settings**: dedicated card in the Settings tab allows changing the
  login password without touching any config files.
- **Error backoff in scheduler**: after 3 consecutive sync failures a subscription is
  automatically skipped for 6 hours. Manual sync always bypasses the backoff.
- **Scheduler respects unconfigured state**: the scheduler now silently skips all checks
  until a Firewalla API key has been saved. Once the key is added via the setup page (or
  Settings), automatic syncing starts on the next 60-second tick — no restart required.
- **`VERSION` file**: single source of truth for the app version. `app.py` reads it at
  startup; no more version strings scattered across multiple files.
- **Shared persistent `httpx` client** (`firewalla_api.py`): one `AsyncClient` is reused
  for the lifetime of the process, keeping TCP/TLS connections alive across API calls
  instead of opening a new connection per request.
- **Preview endpoint timeout**: `GET /api/preview` now enforces a 90-second aggregate
  timeout across all URLs. Slow or unreachable sources no longer hang the request
  indefinitely.
- **401 → login redirect**: a `fetch` interceptor in the dashboard detects expired sessions
  (HTTP 401) and automatically redirects to `/login` instead of showing silent failures.
- **Disabled subscription dimming**: disabled subscriptions are now visually faded in the
  subscriptions table so they are easy to distinguish at a glance.
- **Periodic server version check**: the dashboard now polls `/api/version` every 5 minutes
  and shows a refresh prompt if the server was updated while the page was open.

### Fixed
- **Notes not syncing to Firewalla MSP** (`firewalla_api.py`, `list_manager.py`): the
  `notes` field is now included in the `create_list` and `update_list` API payloads.
- **IPv6 regex too permissive** (`list_manager.py`): replaced the simplified pattern with
  a proper RFC 5952-compliant regex that correctly rejects malformed addresses.
- **`SECRET_KEY` was generated but never used**: it now signs the session cookie, making
  it meaningful. Changing it in `.env` immediately invalidates all active sessions.
- **`aiofiles` dependency was unused**: removed from `requirements.txt`.

### Changed
- **`install.sh`**: removed Firewalla credential prompts. Now only asks for a port number;
  everything else is done in the browser. `--uninstall` flag now delegates to `uninstall.sh`
  so uninstall logic lives in exactly one place.
- **`.env.example`**: updated to reflect that Firewalla credentials are no longer set in
  `.env`; only `HOST`, `PORT`, `MAX_ENTRIES_PER_LIST`, and `SECRET_KEY` remain.
- **Log trim optimisation** (`app.py`): the `DELETE FROM app_logs` cleanup query now runs
  every 50 log writes instead of on every single write, reducing SQLite write pressure.
- **Scheduler SQL** (`scheduler.py`): `_check_due` now tracks `last_success_time` correctly
  (only counting `success`/`skipped` entries, not errors) so the interval is measured from
  the last *successful* sync rather than the last attempt of any kind.
- **`python-multipart`** added to `requirements.txt` (required by FastAPI for HTML form
  handling on the login/setup pages).

---

## [1.0.0] — 2026-04-08

Initial release.

### Features
- FastAPI web application for managing Firewalla MSP target-list subscriptions.
- Automatic scheduled sync (configurable: 1 h / 6 h / 12 h / 24 h / 48 h / 168 h).
- Multi-URL source support: URLs are fetched in parallel and combined.
- Three slot modes: **auto** (dynamic), **fixed** (reserved slots), **rotate** (shuffle
  coverage across a fixed number of slots for lists that exceed the 2000-entry limit).
- ETag / Last-Modified caching for single-URL subscriptions.
- Firewalla API client with retry logic and detailed error logging.
- SQLite database (WAL mode) — no external database required.
- Single-page dashboard: subscriptions table, app logs viewer, settings.
- URL preview analyser — inspect and validate sources before saving.
- `install.sh` — automated Debian/Ubuntu installer (systemd service, venv, firewall rules).
- `uninstall.sh` — clean removal with optional data backup.
