# 🔴 Firewalla Feed Automator

**Automated blocklist subscription manager for Firewalla MSP**

Subscribe to external blocklists (Pi-hole, AdGuard, EasyList, etc.) and automatically sync them to your Firewalla MSP instance. Handles list splitting, change detection, and full lifecycle management.

---

## Features

- **Smart Ingestion** — Fetches, cleans, and deduplicates blocklist entries from any URL
- **Auto-Splitting** — Firewalla's 2000-row limit is handled transparently; large lists become `Name_Part1`, `Name_Part2`, etc.
- **Change Detection** — Uses ETag / Last-Modified headers to skip syncs when the source hasn't changed
- **Dynamic Scheduling** — Per-subscription sync intervals (1h to weekly)
- **Full Lifecycle** — Creates, updates, and deletes Firewalla target lists as a unit
- **Modern Dashboard** — Clean web UI with live status, manual triggers, and sync logs
- **Rate Limiting** — Automatic retry with backoff on Firewalla API 429 responses

---

## Quick Start (Debian/Ubuntu)

```bash
git clone https://github.com/your-org/firewalla-feed-automator
cd firewalla-feed-automator
sudo bash install.sh
```

The installer will:
1. Install Python 3, pip, and venv
2. Create a dedicated system user (`fwautomator`)
3. Set up the virtual environment and install Python deps
4. Prompt for your Firewalla API key and MSP ID
5. Register and start a `systemd` service

Then open: **http://YOUR_IP:8080**

---

## Manual Setup (Development)

```bash
# Clone and enter directory
git clone ...
cd firewalla-feed-automator

# Create virtual environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
nano .env  # Add your FIREWALLA_API_KEY and FIREWALLA_MSP_ID

# Run
uvicorn app:app --host 0.0.0.0 --port 8080 --reload
```

---

## Configuration (`.env`)

| Variable | Description | Default |
|---|---|---|
| `FIREWALLA_API_KEY` | Your MSP API key | *(required)* |
| `FIREWALLA_MSP_ID` | Your MSP ID | *(required)* |
| `FIREWALLA_API_URL` | API base URL | `https://firewalla.io` |
| `PORT` | Web UI port | `8080` |
| `MAX_ENTRIES_PER_LIST` | Row limit per Firewalla list | `2000` |

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    FastAPI Application                   │
│                                                         │
│  ┌──────────────┐   ┌──────────────┐   ┌────────────┐  │
│  │  Dashboard   │   │  REST API    │   │ Scheduler  │  │
│  │  (Jinja2/JS) │   │  /api/*      │   │ (asyncio)  │  │
│  └──────────────┘   └──────────────┘   └─────┬──────┘  │
│                              │               │         │
│                      ┌───────┴───────────────┘         │
│                      ▼                                  │
│              ┌──────────────┐                          │
│              │ ListManager  │  fetch → clean → split   │
│              └──────┬───────┘                          │
│                     │                                  │
│         ┌───────────┼───────────┐                      │
│         ▼           ▼           ▼                      │
│    ┌─────────┐ ┌─────────┐ ┌─────────┐                 │
│    │ SQLite  │ │Firewalla│ │  httpx  │                 │
│    │   DB    │ │  API    │ │ client  │                 │
│    └─────────┘ └─────────┘ └─────────┘                 │
└─────────────────────────────────────────────────────────┘
```

---

## The 2000-Row Split Logic

When a source list exceeds `MAX_ENTRIES_PER_LIST` (default: 2000):

```
Source: 5,400 entries
  → Firewalla: "MyList_Part1"  (2000 entries)
  → Firewalla: "MyList_Part2"  (2000 entries)
  → Firewalla: "MyList_Part3"  (1400 entries)
```

**On shrink** (e.g., list drops from 5,400 to 2,800 entries next sync):
```
  → Updates "MyList_Part1"  (2000 entries)
  → Updates "MyList_Part2"   (800 entries)
  → DELETES "MyList_Part3"  ← automatic cleanup
```

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/health` | App + Firewalla API health |
| `GET` | `/api/subscriptions` | List all subscriptions |
| `POST` | `/api/subscriptions` | Create subscription |
| `PUT` | `/api/subscriptions/{id}` | Update subscription |
| `DELETE` | `/api/subscriptions/{id}` | Delete + remove Firewalla lists |
| `POST` | `/api/subscriptions/{id}/sync` | Manual sync trigger |
| `GET` | `/api/subscriptions/{id}/logs` | Sync history |

Full OpenAPI docs available at `/docs`.

---

## Service Management

```bash
# Status
systemctl status firewalla-feed-automator

# Restart after config changes
systemctl restart firewalla-feed-automator

# View live logs
journalctl -u firewalla-feed-automator -f

# Uninstall
sudo bash install.sh --uninstall
```

---

## Popular Blocklist URLs

```
# Ads & Tracking
https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts
https://adaway.org/hosts.txt
https://v.firebog.net/hosts/AdguardDNS.txt

# Malware & Phishing
https://malware-filter.gitlab.io/malware-filter/phishing-filter-hosts.txt
https://raw.githubusercontent.com/nicholasstephan/malware-domains/main/domains.txt

# Privacy
https://raw.githubusercontent.com/nickcook530/privacy-filter/main/privacy.txt
```
