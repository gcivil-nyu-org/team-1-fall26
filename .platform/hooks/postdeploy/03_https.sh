#!/bin/bash
# Serve the site over HTTPS with a free Let's Encrypt certificate.
# The domains come from the SITE_DOMAINS environment property, primary first,
# e.g. "syncmily.app,www.syncmily.app". Without it, nothing changes.
# Runs after every deploy and config change, because both rebuild /etc/nginx.
# Until a certificate exists (DNS not pointed here yet), the site stays on HTTP.
set -euo pipefail

NGINX_DIR="${NGINX_DIR:-/etc/nginx}"
LIVE_DIR="${LIVE_DIR:-/etc/letsencrypt/live}"
WEBROOT="${WEBROOT:-/var/www/certbot}"
CERTBOT=/opt/certbot/bin/certbot
CERTBOT_REQUIREMENTS="${CERTBOT_REQUIREMENTS:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/certbot-requirements.txt}"

SITE_DOMAINS="${SITE_DOMAINS:-$(/opt/elasticbeanstalk/bin/get-config environment -k SITE_DOMAINS 2>/dev/null || true)}"
if [ -z "$SITE_DOMAINS" ]; then
    echo "SITE_DOMAINS is not set; serving HTTP only."
    exit 0
fi
IFS=',' read -r -a DOMAINS <<< "$SITE_DOMAINS"
PRIMARY="${DOMAINS[0]}"
CERT_DIR="$LIVE_DIR/$PRIMARY"

# Let's Encrypt checks domain ownership over HTTP at /.well-known/acme-challenge/.
mkdir -p "$WEBROOT"
cat > "$NGINX_DIR/conf.d/elasticbeanstalk/02_acme_challenge.conf" <<CONF
location ^~ /.well-known/acme-challenge/ {
    root $WEBROOT;
    default_type text/plain;
}
CONF

if [ -z "${SKIP_CERTBOT:-}" ]; then
    if [ ! -x "$CERTBOT" ]; then
        python3.14 -m venv /opt/certbot
        # Exact versions with hashes: certbot==5.10.0 and everything it needs.
        /opt/certbot/bin/pip install --quiet --require-hashes -r "$CERTBOT_REQUIREMENTS"  # certbot==5.10.0
    fi
    # nginx must already serve the challenge directory before certbot asks for it.
    nginx -t && systemctl reload nginx
    if [ ! -f "$CERT_DIR/fullchain.pem" ]; then
        domain_args=()
        for domain in "${DOMAINS[@]}"; do domain_args+=(-d "$domain"); done
        if ! "$CERTBOT" certonly --webroot -w "$WEBROOT" "${domain_args[@]}" --cert-name "$PRIMARY" \
            --non-interactive --agree-tos --register-unsafely-without-email; then
            echo "Could not get a certificate for $SITE_DOMAINS yet (is DNS pointed here?); serving HTTP only."
            exit 0
        fi
    fi
fi
[ -f "$CERT_DIR/fullchain.pem" ] || { echo "No certificate yet; serving HTTP only."; exit 0; }

# Security headers, written once and included by the server block and by /static (nginx
# drops server-level add_header lines in any location that sets its own, so /static must
# repeat them). The policy lives in this one variable; settings.CSP_POLICY must match it.
# CSP_REPORT_ONLY=true (an EB environment property) sends the same policy as
# Content-Security-Policy-Report-Only instead, for a trial deploy.
CSP_REPORT_ONLY="${CSP_REPORT_ONLY:-$(/opt/elasticbeanstalk/bin/get-config environment -k CSP_REPORT_ONLY 2>/dev/null || true)}"
CSP_LINE=$(cat <<POLICY
add_header Content-Security-Policy "default-src 'none'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; style-src-elem 'self' https://fonts.googleapis.com 'sha256-NmBAHKlY279x2mY0mfnOU0anE9pVNdbkCyLPbCcMu6g='; style-src-attr 'unsafe-inline'; font-src https://fonts.gstatic.com; img-src 'self' data: blob:; connect-src 'self' wss://$PRIMARY https://photon.komoot.io; manifest-src 'self'; form-action 'self' https://accounts.google.com; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; report-uri /csp-report/; report-to csp" always;
POLICY
)
if [ "$CSP_REPORT_ONLY" = "true" ]; then
    CSP_LINE="${CSP_LINE/add_header Content-Security-Policy /add_header Content-Security-Policy-Report-Only }"
fi
mkdir -p "$NGINX_DIR/syncmily"
cat > "$NGINX_DIR/syncmily/security_headers.conf" <<CONF
# Browser features Syncmily never uses stay off. Geolocation is allowed for this site only:
# "Use my location" starts a trip from where the person is. Inline scripts must stay in
# static files. Inline style attributes are allowed; <style> elements are not (500.html is
# allowed by its hash).
add_header Permissions-Policy "camera=(), microphone=(), geolocation=(self), payment=(), usb=()" always;
$CSP_LINE
add_header Reporting-Endpoints 'csp="/csp-report/"' always;
CONF

# Static files keep the same security headers and add their cache rule.
cat > "$NGINX_DIR/syncmily/static_cache.conf" <<CONF
add_header Cache-Control \$syncmily_static_cache;
CONF

# HTTPS server. Mirrors Elastic Beanstalk's own proxy settings, plus the scheme
# header Django needs to know the request was secure.
cat > "$NGINX_DIR/conf.d/syncmily_https.conf" <<CONF
# Static URLs carry a content hash (?v=..., the asset template tag), so a stamped file never
# changes and browsers may keep it for a year. Anything requested without the stamp gets an hour.
map \$arg_v \$syncmily_static_cache {
    ""      "public, max-age=3600";
    default "public, max-age=31536000, immutable";
}

# Request-rate ceilings per client address (this file sits in conf.d, inside the http block).
# Over the limit nginx answers 429. A class on one campus Wi-Fi shares an address, so the
# ceilings fit a room signing in at once.
limit_req_zone \$binary_remote_addr zone=syncmily_auth:10m rate=60r/m;
limit_req_zone \$binary_remote_addr zone=syncmily_scan:10m rate=10r/m;
limit_req_zone \$binary_remote_addr zone=syncmily_writes:10m rate=120r/m;
limit_req_zone \$binary_remote_addr zone=syncmily_ws:10m rate=30r/m;
limit_conn_zone \$binary_remote_addr zone=syncmily_conn:10m;
limit_req_status 429;
limit_conn_status 429;

# The stock "main" format has no timings. This one adds request_time and
# upstream_response_time so slow requests show up in the log.
log_format syncmily_timed '\$remote_addr - \$remote_user [\$time_local] "\$request" '
                          '\$status \$body_bytes_sent "\$http_referer" "\$http_user_agent" '
                          '"\$http_x_forwarded_for" rt=\$request_time urt=\$upstream_response_time';

server {
    listen 443 ssl;
    http2 on;
    server_name ${DOMAINS[*]};
    ssl_certificate     $CERT_DIR/fullchain.pem;
    ssl_certificate_key $CERT_DIR/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    # AEAD-only TLS 1.2 suites (Mozilla "intermediate"): drops the CBC suites testssl flagged (LUCKY13).
    ssl_ciphers         ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305;
    ssl_prefer_server_ciphers off;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_tickets off;
    server_tokens       off;

    include $NGINX_DIR/syncmily/security_headers.conf;
    access_log /var/log/nginx/access.log syncmily_timed;
    # Slow clients cannot hold a worker for long. Over HTTP/2 nginx counts every request as a
    # connection, so there is no server-wide limit_conn; only /ws/ has one.
    client_header_timeout 15;
    client_body_timeout   15;
    send_timeout          30;
    keepalive_timeout     30;
    reset_timedout_connection on;
    # Forms post a few KB. Only the scan upload sets a larger cap below.
    client_max_body_size  128k;

    # Pages, CSS and JS were sent uncompressed (a 72 KB page, 89 KB of CSS). gzip cuts them about
    # 4x. Django masks the CSRF token differently on every response, which keeps BREACH off it.
    gzip            on;
    gzip_vary       on;
    gzip_proxied    any;
    gzip_comp_level 5;
    gzip_min_length 1024;
    gzip_types      text/css application/javascript text/javascript application/json image/svg+xml application/manifest+json text/plain;

    if (\$host != "$PRIMARY") {
        return 301 https://$PRIMARY\$request_uri;
    }

    include $NGINX_DIR/conf.d/elasticbeanstalk/01_block_probes.conf;

    # A size or rate refusal on the JSON paths (scan-ocr, place search) answers JSON, as the
    # page scripts expect, not nginx's HTML page.
    location @json_413 {
        default_type application/json;
        return 413 '{"error": "That upload is too large."}';
    }
    location @json_429 {
        default_type application/json;
        return 429 '{"error": "Too many requests. Try again in a minute."}';
    }

    # Size, rate and upstream failures on page routes answer a short branded page, not
    # nginx's default one. The JSON paths below set their own error_page lines.
    location @html_413 {
        default_type text/html;
        return 413 '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Too large - Syncmily</title><body style="font-family:system-ui,sans-serif;max-width:26rem;margin:4rem auto;padding:0 1rem"><h1>That is too large</h1><p>Send something smaller and try again.</p><p><a href="/">Back to Syncmily</a></p></body></html>';
    }
    location @html_429 {
        default_type text/html;
        return 429 '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Slow down - Syncmily</title><body style="font-family:system-ui,sans-serif;max-width:26rem;margin:4rem auto;padding:0 1rem"><h1>Too many requests</h1><p>Wait a minute and try again.</p><p><a href="/">Back to Syncmily</a></p></body></html>';
    }
    location @html_5xx {
        default_type text/html;
        return 503 '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Try again - Syncmily</title><body style="font-family:system-ui,sans-serif;max-width:26rem;margin:4rem auto;padding:0 1rem"><h1>Syncmily is busy</h1><p>Syncmily could not answer just now. Try again in a minute.</p><p><a href="/">Back to Syncmily</a></p></body></html>';
    }
    error_page 413 @html_413;
    error_page 429 @html_429;
    error_page 502 503 504 @html_5xx;

    # Invite and password-reset links carry a one-time token in the path. They stay out of
    # the access log, so a token never reaches CloudWatch.
    location ~ ^/accounts/(invite|reset)/ {
        access_log off;
        limit_req zone=syncmily_auth burst=60 nodelay;
        proxy_pass          http://127.0.0.1:8000;
        proxy_http_version  1.1;
        proxy_set_header    Host                \$host;
        proxy_set_header    X-Real-IP           \$remote_addr;
        proxy_set_header    X-Forwarded-For     \$proxy_add_x_forwarded_for;
        proxy_set_header    X-Forwarded-Proto   https;
    }

    location ~ ^/accounts/(login|signup|password-reset|two-step|invite)/ {
        limit_req zone=syncmily_auth burst=60 nodelay;
        proxy_pass          http://127.0.0.1:8000;
        proxy_http_version  1.1;
        proxy_set_header    Host                \$host;
        proxy_set_header    X-Real-IP           \$remote_addr;
        proxy_set_header    X-Forwarded-For     \$proxy_add_x_forwarded_for;
        proxy_set_header    X-Forwarded-Proto   https;
    }

    # Receipt and schedule photos promise up to 10 MB; nginx's 1 MB default rejects phone photos.
    location ~ /scan-ocr/ {
        limit_req zone=syncmily_scan burst=10 nodelay;
        client_max_body_size  12m;
        error_page 413 = @json_413;
        error_page 429 = @json_429;
        proxy_pass          http://127.0.0.1:8000;
        proxy_http_version  1.1;
        proxy_set_header    Host                \$host;
        proxy_set_header    X-Real-IP           \$remote_addr;
        proxy_set_header    X-Forwarded-For     \$proxy_add_x_forwarded_for;
        proxy_set_header    X-Forwarded-Proto   https;
    }

    # Place search answers JSON and shares the write ceiling.
    location /events/places/ {
        limit_req zone=syncmily_writes burst=100 nodelay;
        error_page 413 = @json_413;
        error_page 429 = @json_429;
        proxy_pass          http://127.0.0.1:8000;
        proxy_http_version  1.1;
        proxy_set_header    Host                \$host;
        proxy_set_header    X-Real-IP           \$remote_addr;
        proxy_set_header    X-Forwarded-For     \$proxy_add_x_forwarded_for;
        proxy_set_header    X-Forwarded-Proto   https;
    }

    # Chat WebSocket. Handshakes get their own rate zone and a per-address connection cap.
    location /ws/ {
        limit_req zone=syncmily_ws burst=30 nodelay;
        limit_conn syncmily_conn 60;
        client_max_body_size 1k;
        proxy_read_timeout  3600;
        proxy_send_timeout  60;
        proxy_pass          http://127.0.0.1:8000;
        proxy_http_version  1.1;
        proxy_set_header    Connection          \$connection_upgrade;
        proxy_set_header    Upgrade             \$http_upgrade;
        proxy_set_header    Host                \$host;
        proxy_set_header    X-Real-IP           \$remote_addr;
        proxy_set_header    X-Forwarded-For     \$proxy_add_x_forwarded_for;
        proxy_set_header    X-Forwarded-Proto   https;
    }

    location / {
        client_max_body_size 128k;
        limit_req zone=syncmily_writes burst=100 nodelay;
        proxy_pass          http://127.0.0.1:8000;
        proxy_http_version  1.1;
        proxy_set_header    Connection          \$connection_upgrade;
        proxy_set_header    Upgrade             \$http_upgrade;
        proxy_set_header    Host                \$host;
        proxy_set_header    X-Real-IP           \$remote_addr;
        proxy_set_header    X-Forwarded-For     \$proxy_add_x_forwarded_for;
        proxy_set_header    X-Forwarded-Proto   https;
    }

    location /static {
        alias /var/app/current/staticfiles;
        access_log off;
        include $NGINX_DIR/syncmily/security_headers.conf;
        include $NGINX_DIR/syncmily/static_cache.conf;
    }
}
CONF

# Plain HTTP now only answers certificate checks and health checks; everything
# else moves to the HTTPS site.
cat > "$NGINX_DIR/conf.d/elasticbeanstalk/03_https_redirect.conf" <<CONF
set \$redirect_to_https 1;
if (\$request_uri ~ "^/(\.well-known/acme-challenge/|healthz/)") {
    set \$redirect_to_https 0;
}
if (\$redirect_to_https) {
    return 301 https://$PRIMARY\$request_uri;
}
CONF

[ -n "${SKIP_CERTBOT:-}" ] && exit 0
nginx -t && systemctl reload nginx

# Renew twice a day; certbot only replaces certificates close to expiry.
cat > /etc/systemd/system/syncmily-cert-renew.service <<UNIT
[Unit]
Description=Renew the Syncmily HTTPS certificate

[Service]
Type=oneshot
ExecStart=$CERTBOT renew --quiet --webroot -w $WEBROOT --deploy-hook "systemctl reload nginx"
UNIT

cat > /etc/systemd/system/syncmily-cert-renew.timer <<UNIT
[Unit]
Description=Renew the Syncmily HTTPS certificate twice a day

[Timer]
OnCalendar=*-*-* 03,15:00:00
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now syncmily-cert-renew.timer
