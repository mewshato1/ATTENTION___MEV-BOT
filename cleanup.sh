#!/bin/bash
set +e
systemctl stop mev-bot
systemctl disable mev-bot
rm -f /etc/systemd/system/mev-bot.service
rm -rf /etc/systemd/system/mev-bot.service.d
systemctl daemon-reload
systemctl reset-failed
shred -u /root/mwsht_mev_bnb/.env 2>/dev/null
rm -rf /root/mwsht_mev_bnb
echo "=== DONE ==="
if [ -d /root/mwsht_mev_bnb ]; then
  echo "ОШИБКА: папка всё ещё существует"
else
  echo "папка удалена"
fi
systemctl status mev-bot 2>&1 | head -n 3
