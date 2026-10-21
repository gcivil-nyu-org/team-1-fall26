#!/bin/bash
# Refill staticfiles after every deploy. nginx serves /static from it (.ebextensions/01_django.config).
# The same script runs from .platform/confighooks, because config-only redeploys (eb setenv)
# rebuild /var/app/current without container_commands, which left the site without CSS.
set -euo pipefail
cd /var/app/current
set -a; source /opt/elasticbeanstalk/deployment/env; set +a
source /var/app/venv/*/bin/activate
python manage.py collectstatic --noinput
