#!/usr/bin/env bash
# =============================================================================
# deploy.sh — Первоначальная настройка Vultr VPS (Ubuntu 22.04/24.04)
# Запускать на сервере от root: bash deploy.sh
# =============================================================================
set -euo pipefail

REPO_DIR="/root/mwsht_mev_bnb"
BOT_DIR="$REPO_DIR/bot"
VENV_DIR="$REPO_DIR/venv"
SERVICE_NAME="mev-bot"

echo "=== [1/6] Обновление системы ==="
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3.11 python3.11-venv python3.11-dev \
    python3-pip gcc build-essential \
    curl rsync ufw logrotate

# Установить python3.11 как дефолтный python3 (если нужно)
update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 || true

echo "=== [2/6] Создание директорий ==="
mkdir -p "$BOT_DIR"
mkdir -p /var/log/mev-bot

echo "=== [3/6] Python virtualenv ==="
python3.11 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install --no-cache-dir -r "$BOT_DIR/requirements.txt"

echo "=== [4/6] Systemd сервис ==="
cp "$REPO_DIR/mev-bot.service" /etc/systemd/system/mev-bot.service
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

echo "=== [5/6] Logrotate ==="
cat > /etc/logrotate.d/mev-bot << 'EOF'
/root/mwsht_mev_bnb/bot/mev_bot.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF

echo "=== [6/6] Firewall (UFW) ==="
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw --force enable

echo ""
echo "=== Готово. Проверь .env и запусти: ==="
echo "    systemctl start $SERVICE_NAME"
echo "    systemctl status $SERVICE_NAME"
echo "    journalctl -u $SERVICE_NAME -f"
