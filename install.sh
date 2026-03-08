#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Firewalla Feed Automator - Debian Installation Script
# Supports: Debian 10/11/12, Ubuntu 20.04/22.04/24.04
# Usage: sudo bash install.sh
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
# APP_NAME is derived from the folder this script lives in, so renaming the
# project directory (e.g. firewalla-list-automator) works without editing anything.
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
APP_NAME="$(basename "${SCRIPT_DIR}")"
APP_DIR="/opt/${APP_NAME}"
APP_USER="fwautomator"
APP_PORT=8080
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
PYTHON_MIN_VERSION="3.9"

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; ORANGE='\033[0;33m'; GREEN='\033[0;32m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

log()     { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${ORANGE}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

banner() {
  echo -e "${BOLD}"
  cat <<'EOF'
  ╔════════════════════════════════════════════╗
  ║   🔴 Firewalla Feed Automator Installer    ║
  ║      MSP Blocklist Manager v1.0.0          ║
  ╚════════════════════════════════════════════╝
EOF
  echo -e "${NC}"
}

# ── Pre-flight checks ─────────────────────────────────────────────────────────
check_root() {
  if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root. Try: sudo bash install.sh"
  fi
}

check_os() {
  if [[ ! -f /etc/os-release ]]; then
    warn "Cannot detect OS. Proceeding anyway..."
    return
  fi
  source /etc/os-release
  log "Detected OS: ${NAME} ${VERSION_ID:-''}"
  case "$ID" in
    debian|ubuntu|raspbian) success "Supported OS detected" ;;
    *) warn "Untested OS: ${NAME}. Proceeding..." ;;
  esac
}

check_python() {
  local python_cmd=""
  for cmd in python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$cmd" &>/dev/null; then
      python_cmd="$cmd"
      break
    fi
  done

  if [[ -z "$python_cmd" ]]; then
    log "Python 3 not found, will install..."
    INSTALL_PYTHON=true
  else
    local version
    version=$("$python_cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    log "Found Python ${version} at $(command -v $python_cmd)"
    INSTALL_PYTHON=false
    PYTHON_CMD="$python_cmd"
  fi
}

# ── Installation ──────────────────────────────────────────────────────────────
install_system_deps() {
  log "Updating package lists..."
  apt-get update -qq

  log "Installing system dependencies..."
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    curl \
    wget \
    git \
    build-essential \
    libssl-dev \
    libffi-dev \
    ca-certificates \
    systemd \
    2>/dev/null

  success "System dependencies installed"
}

create_app_user() {
  if id "$APP_USER" &>/dev/null; then
    log "User ${APP_USER} already exists"
  else
    log "Creating system user: ${APP_USER}"
    useradd --system --no-create-home --shell /usr/sbin/nologin "$APP_USER"
    success "Created user: ${APP_USER}"
  fi
}

setup_app_directory() {
  log "Setting up application directory: ${APP_DIR}"

  # If reinstalling, back up .env
  if [[ -f "${APP_DIR}/.env" ]]; then
    cp "${APP_DIR}/.env" "/tmp/.env.backup"
    warn "Backed up existing .env to /tmp/.env.backup"
  fi

  # Copy application files
  mkdir -p "${APP_DIR}"
  if [[ -f "${SCRIPT_DIR}/app.py" ]]; then
    cp -r "${SCRIPT_DIR}/." "${APP_DIR}/"
  else
    error "Application files not found in ${SCRIPT_DIR}. Run install.sh from the project directory."
  fi

  # Create required directories
  mkdir -p "${APP_DIR}/data"
  mkdir -p "${APP_DIR}/static/css"
  mkdir -p "${APP_DIR}/static/js"

  # Restore .env if backed up
  if [[ -f "/tmp/.env.backup" ]]; then
    cp "/tmp/.env.backup" "${APP_DIR}/.env"
    success "Restored existing .env configuration"
  fi

  success "Application files installed to ${APP_DIR}"
}

setup_python_env() {
  log "Creating Python virtual environment..."
  python3 -m venv "${APP_DIR}/venv"

  log "Installing Python dependencies..."
  "${APP_DIR}/venv/bin/pip" install --quiet --upgrade pip
  "${APP_DIR}/venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"

  success "Python environment ready"
}

configure_env() {
  if [[ -f "${APP_DIR}/.env" ]]; then
    log ".env file already exists, skipping configuration prompt"
    return
  fi

  log "Initial configuration setup..."
  echo ""
  echo -e "${BOLD}Enter your Firewalla MSP credentials:${NC}"
  echo -e "${ORANGE}(You can also update these later in the web UI Settings tab)${NC}"
  echo ""

  # API Key
  read -rp "  Firewalla API Key (Personal Access Token): " fw_api_key

  # MSP Domain with clear guidance
  echo ""
  echo -e "  ${BOLD}MSP Domain${NC}"
  echo -e "  Your Firewalla portal URL looks like: ${BLUE}https://yourid.firewalla.net${NC}"
  echo -e "  Enter only the domain — ${ORANGE}no https:// prefix needed${NC}"
  echo -e "  Example: ${GREEN}yourid.firewalla.net${NC}"
  echo ""
  read -rp "  MSP Domain: " fw_msp_domain

  # Strip any accidental https:// or http:// prefix, and trailing slash
  fw_msp_domain="${fw_msp_domain#https://}"
  fw_msp_domain="${fw_msp_domain#http://}"
  fw_msp_domain="${fw_msp_domain%/}"

  # Port
  echo ""
  read -rp "  App Port [${APP_PORT}]: " input_port
  APP_PORT="${input_port:-$APP_PORT}"

  local secret_key
  secret_key=$(cat /dev/urandom | tr -dc 'a-zA-Z0-9' | fold -w 48 | head -n 1 || echo "fallback-secret-$(date +%s)")

  cat > "${APP_DIR}/.env" << ENVEOF
# Firewalla Feed Automator Configuration
# Generated: $(date)

FIREWALLA_API_KEY=${fw_api_key}
FIREWALLA_MSP_DOMAIN=${fw_msp_domain}

HOST=0.0.0.0
PORT=${APP_PORT}
MAX_ENTRIES_PER_LIST=2000
SECRET_KEY=${secret_key}
ENVEOF

  chmod 600 "${APP_DIR}/.env"
  success ".env saved — domain: ${fw_msp_domain}"
}

set_permissions() {
  log "Setting file permissions..."
  chown -R "$APP_USER:$APP_USER" "$APP_DIR"
  chmod -R 750 "$APP_DIR"
  chmod 600 "${APP_DIR}/.env" 2>/dev/null || true
  success "Permissions set"
}

install_systemd_service() {
  log "Installing systemd service..."

  local port
  port=$(grep "^PORT=" "${APP_DIR}/.env" 2>/dev/null | cut -d= -f2 | tr -d ' ' || echo "$APP_PORT")

  cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Firewalla Feed Automator - MSP Blocklist Manager
Documentation=https://github.com/YOUR_USERNAME/firewalla-feed-automator
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port ${port} --workers 1
ExecReload=/bin/kill -HUP \$MAINPID

# Restart policy
Restart=on-failure
RestartSec=5
StartLimitInterval=60
StartLimitBurst=5

# Security hardening
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=${APP_DIR}/data
ProtectHome=yes

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${APP_NAME}

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "$APP_NAME"
  systemctl restart "$APP_NAME" || systemctl start "$APP_NAME"

  success "Systemd service installed and started"
}

setup_firewall() {
  local port
  port=$(grep "^PORT=" "${APP_DIR}/.env" 2>/dev/null | cut -d= -f2 | tr -d ' ' || echo "$APP_PORT")

  if command -v ufw &>/dev/null; then
    log "Configuring UFW firewall for port ${port}..."
    ufw allow "${port}/tcp" comment "Firewalla Feed Automator" 2>/dev/null || true
    success "UFW rule added for port ${port}"
  elif command -v firewall-cmd &>/dev/null; then
    firewall-cmd --permanent --add-port="${port}/tcp" 2>/dev/null || true
    firewall-cmd --reload 2>/dev/null || true
    success "firewalld rule added for port ${port}"
  else
    warn "No firewall manager detected. Manually open port ${port} if needed."
  fi
}

print_summary() {
  local port
  port=$(grep "^PORT=" "${APP_DIR}/.env" 2>/dev/null | cut -d= -f2 | tr -d ' ' || echo "$APP_PORT")
  local ip
  ip=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "YOUR_SERVER_IP")

  echo ""
  echo -e "${GREEN}${BOLD}════════════════════════════════════════════${NC}"
  echo -e "${GREEN}${BOLD}  ✓ Installation Complete!${NC}"
  echo -e "${GREEN}${BOLD}════════════════════════════════════════════${NC}"
  echo ""
  echo -e "  ${BOLD}Dashboard:${NC}  http://${ip}:${port}"
  echo -e "  ${BOLD}API Docs:${NC}   http://${ip}:${port}/docs"
  echo ""
  echo -e "  ${BOLD}Manage service:${NC}"
  echo -e "    systemctl status  ${APP_NAME}"
  echo -e "    systemctl restart ${APP_NAME}"
  echo -e "    journalctl -u ${APP_NAME} -f"
  echo ""
  echo -e "  ${BOLD}Config file:${NC}  ${APP_DIR}/.env"
  echo ""
  echo -e "${ORANGE}  ⚠  Update credentials anytime in the web UI → Settings tab${NC}"
  echo ""
}

# ── Uninstall ─────────────────────────────────────────────────────────────────
uninstall() {
  log "Uninstalling ${APP_NAME}..."
  systemctl stop "$APP_NAME" 2>/dev/null || true
  systemctl disable "$APP_NAME" 2>/dev/null || true
  rm -f "$SERVICE_FILE"
  systemctl daemon-reload
  
  read -rp "Delete application data in ${APP_DIR}? [y/N] " confirm
  if [[ "$confirm" =~ ^[Yy]$ ]]; then
    rm -rf "$APP_DIR"
    userdel "$APP_USER" 2>/dev/null || true
    success "Fully uninstalled"
  else
    success "Service removed. Data kept at ${APP_DIR}"
  fi
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
  banner

  # Handle uninstall flag
  if [[ "${1:-}" == "--uninstall" ]]; then
    check_root
    uninstall
    exit 0
  fi

  check_root
  check_os
  check_python
  install_system_deps
  create_app_user
  setup_app_directory
  setup_python_env
  configure_env
  set_permissions
  install_systemd_service
  setup_firewall
  print_summary
}

main "$@"
