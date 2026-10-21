#!/bin/bash
# Import Google Calendar events every 15 minutes with a systemd timer.
set -euo pipefail

cat > /etc/systemd/system/syncmily-calendar-sync.service <<'UNIT'
[Unit]
Description=Import Google Calendar events into Syncmily

[Service]
Type=oneshot
User=webapp
WorkingDirectory=/var/app/current
EnvironmentFile=/opt/elasticbeanstalk/deployment/env
ExecStart=/bin/bash -c 'source /var/app/venv/*/bin/activate && python manage.py sync_calendars'
UNIT

cat > /etc/systemd/system/syncmily-calendar-sync.timer <<'UNIT'
[Unit]
Description=Run the Syncmily calendar import every 15 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now syncmily-calendar-sync.timer
