#!/bin/bash
# Verify OCR runtime readiness inside the deployed Django virtualenv after deploy.
# Clearly distinguishes core app deployment health from OCR runtime availability.
set -euo pipefail

cd /var/app/current
set -a; source /opt/elasticbeanstalk/deployment/env; set +a
source /var/app/venv/*/bin/activate

REQUIRE_OCR_READY="${REQUIRE_OCR_READY:-false}"

if output="$(python manage.py check_ocr 2>&1)"; then
    echo "$output"
    if command -v logger >/dev/null 2>&1; then
        logger -t syncmily-ocr "$output" || true
    fi
    exit 0
fi

echo "ERROR: $output" >&2
if command -v logger >/dev/null 2>&1; then
    logger -t syncmily-ocr "ERROR: $output" || true
fi

if [ "$REQUIRE_OCR_READY" = "true" ]; then
    exit 1
fi

echo "WARNING: Application deployed, but OCR_READINESS=unavailable — Scan Event will stay disabled." >&2
exit 0
