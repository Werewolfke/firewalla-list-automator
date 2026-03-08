#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Firewalla Feed Automator - Uninstaller
# Removes the service, app files, and system user.
# Does NOT remove system packages (python3, pip, etc.)
# Usage: sudo bash uninstall.sh
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# APP_NAME derived from this script's folder — works regardless of what you named it
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
APP_NAME="$(basename "${SCRIPT_DIR}")"
APP_DIR="/opt/${APP_NAME}"
APP_USER="fwautomator"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
BACKUP_DIR="/tmp/${APP_NAME}-backup-$(date +%Y%m%d-%H%M%S)"

RED='\033[0;31m'; ORANGE='\033[0;33m'; GREEN='\033[0;32m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

log()     { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${ORANGE}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

if [[ $EUID -ne 0 ]]; then
  error "This script must be run as root. Try: sudo bash uninstall.sh"
fi

echo -e "${BOLD}"
cat <<'EOF'
  ╔════════════════════════════════════════════╗
  ║   🔴 Firewalla Feed Automator             ║
  ║      Uninstaller                          ║
  ╚════════════════════════════════════════════╝
EOF
echo -e "${NC}"

echo -e "This will remove:"
echo -e "  • Systemd service  (${APP_NAME})"
echo -e "  • App files        (${APP_DIR})"
echo -e "  • System user      (${APP_USER})"
echo -e "  • Firewall rule    (if applicable)"
echo ""
echo -e "${ORANGE}System packages (python3, pip, etc.) will NOT be touched.${NC}"
echo ""

read -rp "Continue with uninstall? [y/N] " confirm
if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
  echo "Aborted."
  exit 0
fi
echo ""

# ── Offer to back up data ─────────────────────────────────────────────────────
if [[ -d "${APP_DIR}/data" ]]; then
  read -rp "Back up your subscription database to ${BACKUP_DIR} first? [Y/n] " do_backup
  if [[ ! "$do_backup" =~ ^[Nn]$ ]]; then
    mkdir -p "$BACKUP_DIR"
    cp -r "${APP_DIR}/data" "$BACKUP_DIR/"
    [[ -f "${APP_DIR}/.env" ]] && cp "${APP_DIR}/.env" "$BACKUP_DIR/"
    success "Data backed up to ${BACKUP_DIR}"
  fi
fi

# ── Stop and disable service ──────────────────────────────────────────────────
if systemctl is-active --quiet "$APP_NAME" 2>/dev/null; then
  log "Stopping service..."
  systemctl stop "$APP_NAME"
  success "Service stopped"
fi

if systemctl is-enabled --quiet "$APP_NAME" 2>/dev/null; then
  log "Disabling service..."
  systemctl disable "$APP_NAME"
  success "Service disabled"
fi

# ── Remove service file ───────────────────────────────────────────────────────
if [[ -f "$SERVICE_FILE" ]]; then
  log "Removing service file..."
  rm -f "$SERVICE_FILE"
  systemctl daemon-reload
  success "Service file removed"
fi

# ── Remove firewall rule ──────────────────────────────────────────────────────
PORT=$(grep "^PORT=" "${APP_DIR}/.env" 2>/dev/null | cut -d= -f2 | tr -d ' ' || echo "8080")
if command -v ufw &>/dev/null; then
  log "Removing UFW rule for port ${PORT}..."
  ufw delete allow "${PORT}/tcp" 2>/dev/null && success "UFW rule removed" || warn "No UFW rule found for port ${PORT}"
elif command -v firewall-cmd &>/dev/null; then
  firewall-cmd --permanent --remove-port="${PORT}/tcp" 2>/dev/null || true
  firewall-cmd --reload 2>/dev/null || true
  success "firewalld rule removed"
fi

# ── Remove app directory ──────────────────────────────────────────────────────
if [[ -d "$APP_DIR" ]]; then
  log "Removing application directory ${APP_DIR}..."
  rm -rf "$APP_DIR"
  success "Application files removed"
fi

# ── Remove system user ────────────────────────────────────────────────────────
if id "$APP_USER" &>/dev/null; then
  log "Removing system user ${APP_USER}..."
  userdel "$APP_USER" 2>/dev/null
  success "User ${APP_USER} removed"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}════════════════════════════════════════════${NC}"
echo -e "${GREEN}${BOLD}  ✓ Uninstall complete${NC}"
echo -e "${GREEN}${BOLD}════════════════════════════════════════════${NC}"
echo ""
if [[ -d "$BACKUP_DIR" ]]; then
  echo -e "  Your data was backed up to: ${BLUE}${BACKUP_DIR}${NC}"
  echo ""
fi
echo -e "  System packages were not modified."
echo ""
