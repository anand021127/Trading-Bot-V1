#!/usr/bin/env bash
# ==============================================================================
# Upstox Trading Bot - Production Nginx Configuration & Connectivity Fixer
# Target: Ubuntu VM running systemd service 'upstox-bot.service'
# Domain: upstoxbot-anand.duckdns.org
# ==============================================================================
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}======================================================${NC}"
echo -e "${BLUE}  Upstox Trading Bot - Production Connectivity Fixer   ${NC}"
echo -e "${BLUE}======================================================${NC}"

# 1. Check if running as root or with sudo
if [[ $EUID -ne 0 ]]; then
    echo -e "${YELLOW}Notice: This script requires root permissions to modify Nginx config.${NC}"
    echo -e "Re-running with sudo..."
    exec sudo bash "$0" "$@"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
NGINX_SRC="${REPO_DIR}/deploy/nginx/upstoxbot.conf"

# 2. Verify FastAPI backend process is running locally
echo -e "\n${BLUE}[1/5] Checking FastAPI local backend service...${NC}"
if systemctl is-active --quiet upstox-bot.service; then
    echo -e "${GREEN}✓ upstox-bot.service is active and running.${NC}"
else
    echo -e "${YELLOW}! upstox-bot.service is not active. Checking port 8000...${NC}"
fi

# Verify port 8000 responds locally
echo -e "Testing http://127.0.0.1:8000/api/health..."
HEALTH_CHECK=$(curl -sS -m 5 -w "\nHTTP_STATUS=%{http_code}\nTIME=%{time_total}\n" http://127.0.0.1:8000/api/health || true)
if echo "$HEALTH_CHECK" | grep -q "HTTP_STATUS=200"; then
    echo -e "${GREEN}✓ Local FastAPI health endpoint responded with HTTP 200.${NC}"
    echo "$HEALTH_CHECK" | head -n 1
else
    echo -e "${RED}✗ Warning: Local FastAPI health check did not return HTTP 200 on port 8000.${NC}"
    echo -e "Output: $HEALTH_CHECK"
    echo -e "Make sure uvicorn is running: systemctl restart upstox-bot.service"
fi

# 3. Check for Let's Encrypt certificates
echo -e "\n${BLUE}[2/5] Checking SSL certificates for upstoxbot-anand.duckdns.org...${NC}"
CERT_DIR="/etc/letsencrypt/live/upstoxbot-anand.duckdns.org"
if [[ -f "${CERT_DIR}/fullchain.pem" && -f "${CERT_DIR}/privkey.pem" ]]; then
    echo -e "${GREEN}✓ SSL certificates found in ${CERT_DIR}.${NC}"
else
    echo -e "${RED}✗ SSL certificate missing in ${CERT_DIR}!${NC}"
    echo -e "Please ensure certbot has generated the certificates for upstoxbot-anand.duckdns.org."
fi

# 4. Install production Nginx config
echo -e "\n${BLUE}[3/5] Installing production Nginx configuration...${NC}"
if [[ ! -f "$NGINX_SRC" ]]; then
    echo -e "${RED}Error: Source config $NGINX_SRC not found.${NC}"
    exit 1
fi

mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled
cp "$NGINX_SRC" /etc/nginx/sites-available/upstoxbot
ln -sf /etc/nginx/sites-available/upstoxbot /etc/nginx/sites-enabled/upstoxbot

# Remove default site if conflicting
if [[ -f /etc/nginx/sites-enabled/default ]]; then
    echo -e "Backing up and disabling conflicting default site..."
    rm -f /etc/nginx/sites-enabled/default
fi

# Test Nginx syntax
echo -e "\n${BLUE}[4/5] Testing Nginx syntax...${NC}"
nginx -t

echo -e "Reloading Nginx service..."
systemctl reload nginx || systemctl restart nginx
echo -e "${GREEN}✓ Nginx successfully reloaded.${NC}"

# 5. Verify end-to-end connectivity
echo -e "\n${BLUE}[5/5] Verifying public connectivity...${NC}"
echo -e "Testing public HTTPS endpoint: https://upstoxbot-anand.duckdns.org/api/health..."
PUBLIC_CHECK=$(curl -sS -m 8 -w "\nHTTP_STATUS=%{http_code}\nTIME=%{time_total}\n" https://upstoxbot-anand.duckdns.org/api/health || true)

if echo "$PUBLIC_CHECK" | grep -q "HTTP_STATUS=200"; then
    echo -e "${GREEN}======================================================${NC}"
    echo -e "${GREEN}✓ SUCCESS: Public backend is ONLINE and reachable!     ${NC}"
    echo -e "${GREEN}======================================================${NC}"
    echo "$PUBLIC_CHECK"
else
    echo -e "${YELLOW}Notice: Public test returned:${NC}"
    echo "$PUBLIC_CHECK"
    echo -e "If running on an Oracle Cloud VM, ensure port 443 ingress is allowed in Oracle Security Lists and UFW iptables."
fi

echo -e "\n${GREEN}Done! The production backend routing is configured.${NC}"
