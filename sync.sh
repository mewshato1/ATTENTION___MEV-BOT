#!/usr/bin/env bash
# =============================================================================
# sync.sh — Перенос файлов на Vultr VPS через tar + scp (работает в Git Bash)
# Использование: bash sync.sh <IP_VPS>
# =============================================================================
set -euo pipefail

VPS_IP="${1:?Укажи IP VPS: bash sync.sh <IP>}"
VPS_USER="root"
REMOTE_DIR="/root/mwsht_mev_bnb"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"
ARCHIVE="/tmp/mev_bot_deploy.tar.gz"

echo ">>> Упаковка файлов ..."
cd "$LOCAL_DIR"
tar \
    --exclude='./bot/__pycache__' \
    --exclude='./bot/*.pyc' \
    --exclude='./bot/*.pyo' \
    --exclude='./bot/mev_bot.log' \
    --exclude='./bot/pair_addresses.json' \
    --exclude='./hardhat/node_modules' \
    --exclude='./venv' \
    --exclude='./.git' \
    --exclude='./.env' \
    --exclude='./*.bat' \
    -czf "$ARCHIVE" .

echo ">>> Загрузка архива на VPS ($VPS_IP) ..."
scp "$ARCHIVE" "$VPS_USER@$VPS_IP:/tmp/mev_bot_deploy.tar.gz"

echo ">>> Распаковка на VPS ..."
ssh "$VPS_USER@$VPS_IP" "
    mkdir -p $REMOTE_DIR
    tar -xzf /tmp/mev_bot_deploy.tar.gz -C $REMOTE_DIR
    rm /tmp/mev_bot_deploy.tar.gz
"

echo ">>> Копирование .env ..."
scp "$LOCAL_DIR/.env" "$VPS_USER@$VPS_IP:$REMOTE_DIR/.env"
ssh "$VPS_USER@$VPS_IP" "chmod 600 $REMOTE_DIR/.env"

rm -f "$ARCHIVE"

echo ""
echo ">>> Готово! Теперь на VPS запусти:"
echo "    bash $REMOTE_DIR/deploy.sh"
