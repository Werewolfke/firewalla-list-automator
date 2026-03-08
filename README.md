# 🔴 Firewalla Feed Automator

**Automated blocklist subscription manager for [Firewalla MSP](https://firewalla.com/msp)**

Subscribe to external blocklists (Pi-hole, AdGuard, EasyList format, etc.) and automatically sync them to your Firewalla MSP target lists. Handles splitting, deduplication, change detection, and full lifecycle management — all from a clean web UI.

![Version](https://img.shields.io/badge/version-1.0.0-red)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Features

- **Multi-source subscriptions** — Combine multiple blocklist URLs into one subscription; entries are deduplicated across sources
- **Three slot modes** — Auto (dynamic), Fixed (permanent slots, safe for group assignment), or Rotating (partial coverage, shuffled each sync)
- **URL preview** — Analyse any URL before committing: see total entry count, minimum slots needed, and sample entries
- **Smart ingestion** — Handles hosts-file format (`0.0.0.0 domain.com`), AdGuard/Pi-hole comments, inline comments, IPv4/IPv6/CIDR, auto-deduplication
- **Change detection** — ETag / Last-Modified caching skips syncs when the source hasn't changed
- **Auto-split** — Firewalla's 2000-entry limit is handled transparently; large lists become `Name_Part1`, `Name_Part2`, etc.
- **Reconciliation** — Cross-checks local DB against live Firewalla lists before every sync; adopts existing lists by name to prevent duplicates
- **Per-subscription scheduling** — Sync intervals from 1 hour to weekly, per subscription
- **Web dashboard** — Live status, manual sync triggers, sync logs, app logs, and settings — all in-browser
- **Version indicator** — Header badge shows the running version; prompts a page refresh if the server has been updated

---

## Requirements

- Debian 10/11/12 or Ubuntu 20.04/22.04/24.04 (other distros should work)
- Python 3.9+
- A Firewalla MSP account with a Personal Access Token

---

## Quick Start

```bash
git clone git clone https://github.com/Werewolfke/firewalla-list-automator
cd firewalla-list-automator
sudo bash install.sh
```

The installer will:
1. Install Python 3, pip, and venv (if not present)
2. Create a dedicated system user (`fwautomator`)
3. Set up the Python virtual environment and install dependencies
4. Prompt for your **Firewalla API key** and **MSP domain**
5. Register and start a `systemd` service on port `8080`

Then open: **`http://YOUR_SERVER_IP:8080`**
(DONT PUBLISH THIS ONLINE! Keep this LOCAL!)

---

## Getting your Firewalla credentials

**MSP Domain**

Log in to your Firewalla MSP portal. Your URL will look like:
```
https://yourid.firewalla.net
```
Use only the domain part — `yourid.firewalla.net` — no `https://` prefix.

**Personal Access Token (API Key)**

In the MSP portal → *Profile* → *Personal Access Tokens* → *Generate new token*.
Give it read/write access to target lists.

---

## Manual / Development Setup

```bash
git clone https://github.com/Werewolfke/firewalla-list-automator
cd firewalla-list-automator

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env — set FIREWALLA_API_KEY and FIREWALLA_MSP_DOMAIN

uvicorn app:app --host 0.0.0.0 --port 8080 --reload
```

---

## Configuration

Settings are stored in the SQLite database and can be updated live from the **Settings tab** in the web UI — no restart required. They can also be pre-seeded via `.env`:

| Variable | Description | Default |
|---|---|---|
| `FIREWALLA_API_KEY` | Personal Access Token | *(set in UI)* |
| `FIREWALLA_MSP_DOMAIN` | e.g. `yourid.firewalla.net` | *(set in UI)* |
| `PORT` | Web UI port | `8080` |
| `MAX_ENTRIES_PER_LIST` | Row limit per Firewalla list | `2000` |

---

## Slot Allocation Modes

Firewalla requires you to manually assign target lists to groups/LANs. The slot mode controls how lists are managed across syncs:

| Mode | Behaviour | Use when |
|---|---|---|
| **Auto** | Lists grow and shrink dynamically | Simple setups, don't mind re-assigning groups |
| **Fixed** | Exactly N lists, created once, never deleted | You want permanent group assignments — set N with buffer room for growth |
| **Rotate** | N lists, full source shuffled each sync | Very large lists with limited slots — different entries blocked each cycle |

**Fixed mode example:** Source has 26,000 entries (13 slots minimum). Set 16 fixed slots for headroom. Group assignments are set once and never need updating again.

**Rotate mode example:** Source has 26,000 entries but you only want 15 slots. Each sync shuffles the full list and distributes evenly (~1,733 entries/slot). Coverage rotates so different entries are blocked each cycle.

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
│              ┌──────────────┐                           │
│              │ ListManager  │  fetch → clean → sync     │
│              └──────┬───────┘                           │
│                     │                                   │
│         ┌───────────┼───────────┐                       │
│         ▼           ▼           ▼                       │
│    ┌─────────┐ ┌─────────┐ ┌─────────┐                  │
│    │ SQLite  │ │Firewalla│ │  httpx  │                  │
│    │   DB    │ │  API    │ │ client  │                  │
│    └─────────┘ └─────────┘ └─────────┘                  │
└─────────────────────────────────────────────────────────┘
```

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/version` | Running app version |
| `GET` | `/api/health` | App + Firewalla API connection status |
| `GET` | `/api/preview?url=…` | Analyse a URL without saving |
| `GET` | `/api/subscriptions` | List all subscriptions |
| `POST` | `/api/subscriptions` | Create subscription |
| `PUT` | `/api/subscriptions/{id}` | Update subscription |
| `DELETE` | `/api/subscriptions/{id}` | Delete + remove Firewalla lists |
| `POST` | `/api/subscriptions/{id}/sync` | Trigger manual sync |
| `GET` | `/api/subscriptions/{id}/logs` | Sync history |
| `GET` | `/api/logs` | Application logs |
| `GET` | `/api/settings` | Current settings |
| `PUT` | `/api/settings` | Save settings + test connection |

Interactive API docs: **`http://YOUR_IP:8080/docs`**

---

## Service Management

```bash
# Status
systemctl status firewalla-list-automator

# Restart (e.g. after a manual file update)
systemctl restart firewalla-list-automator

# Live logs
journalctl -u firewalla-list-automator -f

# Uninstall
sudo bash uninstall.sh
```

---

## Updating

```bash
cd /opt/firewalla-list-automator
git pull
sudo systemctl restart firewalla-list-automator
```

The version badge in the top-right of the dashboard will show a yellow **⚠ refresh?** if the page you have open is older than the running server — just hit **Ctrl+Shift+R** to hard-refresh.

When releasing a new version, bump `APP_VERSION` in `app.py` and `_localVersion` in `dashboard.html` to match.

---

## Popular Blocklist URLs

```
# Ads & Tracking
https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts
https://adaway.org/hosts.txt
https://v.firebog.net/hosts/AdguardDNS.txt
https://raw.githubusercontent.com/hagezi/dns-blocklists/main/hosts/multi.txt

# Malware & Phishing
https://malware-filter.gitlab.io/malware-filter/phishing-filter-hosts.txt

# Malicious IPs
https://raw.githubusercontent.com/romainmarcoux/malicious-ip/refs/heads/main/full-40k.txt
https://github.com/firehol/blocklist-ipsets
```

---

<img width="1302" height="540" alt="image" src="https://github.com/user-attachments/assets/ed9a3434-a469-4607-943b-bf0fc3669983" />



## Tech Stack

| Component | Technology |
|---|---|
| Backend | Python 3, FastAPI, uvicorn |
| Frontend | Jinja2, Tailwind CSS (CDN), vanilla JS |
| Database | SQLite (WAL mode) |
| HTTP client | httpx (async) |
| Service | systemd |

---

## License

MIT — see [LICENSE](LICENSE) for details.

---

## Honesty

This project is build with ai. (does not contain ai or external api calls.)
It has been tested, and does not do anything that it is not expected to do.
Can be completely isolated except for the firewalla api calls and list sources. 
Bugs can ofcourse occur, always welcome to report them. 

This project was made for myself, and I do use it.
Sharing this under MIT license for people who want to build further on it (as I am nowhere near a expert.)

Don't install this directly on ur firewalla, install this on a vm or other sort of virtualised environment. 
Ofcourse u are free to do as u want, but firewalls should be used as firewalls in my opinion. Anything u install extra on it, is another security breach waiting to happen. 