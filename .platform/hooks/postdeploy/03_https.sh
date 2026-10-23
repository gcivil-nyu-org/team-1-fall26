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
        /opt/certbot/bin/pip install --quiet --upgrade pip certbot
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

# HTTPS server. Mirrors Elastic Beanstalk's own proxy settings, plus the scheme
# header Django needs to know the request was secure.
cat > "$NGINX_DIR/conf.d/syncmily_https.conf" <<CONF
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

    # Browser features Syncmily never uses stay off. The CSP is enforced: every page was checked
    # in WebKit with it enforced (0 violations, chat connects); inline scripts must stay in static files.
    add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=(), usb=()" always;
    add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: https://*.googleusercontent.com; connect-src 'self' wss://$PRIMARY; frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://accounts.google.com; object-src 'none'" always;
    access_log /var/log/nginx/access.log main;
    client_header_timeout 60;
    client_body_timeout   60;
    keepalive_timeout     60;

    if (\$host != "$PRIMARY") {
        return 301 https://$PRIMARY\$request_uri;
    }

    include $NGINX_DIR/conf.d/elasticbeanstalk/01_block_probes.conf;

    location / {
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
