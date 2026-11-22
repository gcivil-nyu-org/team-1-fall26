#!/bin/bash
# Delete expired data every night with a systemd timer (spec 4.5): expired sessions,
# django-axes access logs older than 30 days, and the app's own retention job
# (rate counters, old chat, stale invites, used or expired email confirmations).
set -euo pipefail

cat > /etc/systemd/system/syncmily-purge.service <<'UNIT'
[Unit]
Description=Delete expired Syncmily data

[Service]
Type=oneshot
User=webapp
# systemd turns the timeout off for oneshot units; set one so a stuck run cannot block later runs.
TimeoutStartSec=1800
WorkingDirectory=/var/app/current
EnvironmentFile=/opt/elasticbeanstalk/deployment/env
ExecStart=/bin/bash -c 'source /var/app/venv/*/bin/activate && python manage.py clearsessions && python manage.py axes_reset_logs --age 30 && python manage.py purge_expired'
UNIT

cat > /etc/systemd/system/syncmily-purge.timer <<'UNIT'
[Unit]
Description=Run the Syncmily data purge every night

[Timer]
OnCalendar=*-*-* 04:30:00
RandomizedDelaySec=15min
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now syncmily-purge.timer
