import os
import sys
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

from syncmily import limits

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]

INSTALLED_APPS = [
    "checklists.apps.ChecklistsConfig",
    "daphne",
    "accounts.apps.AccountsConfig",
    "events.apps.EventsConfig",
    "chat.apps.ChatConfig",
    "calendars.apps.CalendarsConfig",
    # The project package: shared template tags (safety), models and commands.
    "syncmily.apps.SyncmilyConfig",
    # The admin, with staff two-step sign-in (accounts/admin_site.py).
    "accounts.admin_config.SyncmilyAdminConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "axes",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "django_otp.plugins.otp_static",
]

MIDDLEWARE = [
    "syncmily.health.HealthCheckMiddleware",
    "syncmily.middleware.RequestContextMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "syncmily.middleware.RequestBodyLimitMiddleware",
    "syncmily.middleware.ContentSecurityPolicyMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Marks sessions that passed the two-step code; the admin requires it.
    "django_otp.middleware.OTPMiddleware",
    "syncmily.middleware.WriteRateLimitMiddleware",
    "syncmily.middleware.PrivatePageCacheMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Last, so it sees every login attempt and can answer with the lockout page.
    "axes.middleware.AxesMiddleware",
]

ROOT_URLCONF = "syncmily.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "accounts.context_processors.shell",
            ],
        },
    },
]

WSGI_APPLICATION = "syncmily.wsgi.application"
ASGI_APPLICATION = "syncmily.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "syncmily"),
        "USER": os.environ.get("POSTGRES_USER", "postgres"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        # Connections are reused through a pool (OPTIONS["pool"]). Django requires
        # CONN_MAX_AGE = 0 with a pool. Under ASGI each request runs in its own thread,
        # so per-thread persistent connections pile up until Postgres refuses new ones.
        "CONN_MAX_AGE": 0,
        "OPTIONS": {
            # One shared pool per process. A request that waits more than 5 s for a
            # free connection gets a 503 with Retry-After (syncmily.errors.handler500).
            # Keep max_size times the number of processes below the RDS max_connections.
            "pool": {
                # 0: open connections on demand, so management commands and a fresh
                # setup without the database yet do not retry in the background.
                "min_size": 0,
                "max_size": int(os.environ.get("POSTGRES_POOL_MAX_SIZE", "20")),
                "timeout": 5,
                "max_idle": 300,
            },
            "connect_timeout": 5,
            # Cap one statement at 25 s and a lock wait at 5 s, so a stuck query or a
            # blocked migration cannot stall every request.
            "options": "-c statement_timeout=25000 -c lock_timeout=5000",
            # Verify the RDS certificate and host name. libpq ignores sslmode on a Unix
            # socket, so local and CI runs that connect over a socket still work.
            "sslmode": os.environ.get("POSTGRES_SSLMODE", "verify-full"),
            "sslrootcert": os.environ.get(
                "POSTGRES_SSLROOTCERT",
                str(BASE_DIR / "syncmily" / "certs" / "rds-global-bundle.pem"),
            ),
        },
    },
}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
        )
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = (  # the default for display and new events; stored times stay UTC (USE_TZ)
    "America/New_York"
)
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

# On unless DJANGO_HTTPS=false, so a missing variable fails closed. Local runs and the
# test job set DJANGO_HTTPS=false. nginx on the instance terminates TLS and tells
# Django the original scheme in X-Forwarded-Proto
# (.platform/hooks/postdeploy/03_https.sh).
if os.environ.get("DJANGO_HTTPS", "true").lower() != "false":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # One year, the minimum for browser preload lists. Safe for subdomains: .app is an
    # HSTS-preloaded TLD, so browsers already refuse plain HTTP on every *.syncmily.app.
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

# Brute-force protection: 5 wrong passwords for one username from one IP locks
# that pair out for 30 minutes, on the app login and the admin alike. Locking the
# pair, not the IP alone, keeps one person's typos from locking out a family on
# the same Wi-Fi. Unlock early with `python manage.py axes_reset_username <name>`.
AUTHENTICATION_BACKENDS = [
    "syncmily.auth_backends.LockoutBackend",
    "django.contrib.auth.backends.ModelBackend",
]
# Per-user/IP limits on expensive or spammable actions (syncmily/ratelimit.py).
# Off under `manage.py test` so existing tests can loop; rate-limit tests
# turn it back on with override_settings(RATELIMIT_ENABLED=True).
RATELIMIT_ENABLED = (
    os.environ.get(
        "RATELIMIT_ENABLED", "false" if "test" in sys.argv[1:2] else "true"
    ).lower()
    == "true"
)

# Logging (spec 10.2): one key=value line per record on stdout, with the request id,
# user id, client address and route. syncmily.logs cleans every message, so no value can
# forge a line or leak a token. Quiet under `manage.py test` unless LOG_LEVEL is set.
LOG_LEVEL = os.environ.get(
    "DJANGO_LOG_LEVEL", "CRITICAL" if "test" in sys.argv[1:2] else "INFO"
).upper()
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"context": {"()": "syncmily.logs.ContextFilter"}},
    "formatters": {
        "keyvalue": {
            "()": "syncmily.logs.SafeFormatter",
            "format": (
                "time=%(asctime)s level=%(levelname)s logger=%(name)s "
                "request_id=%(request_id)s user_id=%(user_id)s "
                "client_ip=%(client_ip)s route=%(route)s "
                "event=%(event)s outcome=%(outcome)s msg=%(message)s"
            ),
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "keyvalue",
            "filters": ["context"],
            "level": LOG_LEVEL,
        }
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}

AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = timedelta(minutes=30)
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_TEMPLATE = "accounts/locked_out.html"
# Record at most a username's length, so a huge value is a normal failure (H-FUZZ-04).
AXES_USERNAME_CALLABLE = "syncmily.auth_backends.axes_username"
# nginx on the instance appends the client's address to X-Forwarded-For, so the
# right-most entry is the one a visitor cannot forge.
AXES_IPWARE_META_PRECEDENCE_ORDER = ["HTTP_X_FORWARDED_FOR", "REMOTE_ADDR"]
AXES_IPWARE_PROXY_ORDER = "right-most"

# Keep the admin off the well-known /admin/ path in production.
ADMIN_PATH = os.environ.get("DJANGO_ADMIN_PATH", "admin/").strip("/") + "/"

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "accounts:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

# Real mail by default. A missing variable must never write invite and reset links to
# the server log. For local work, set DJANGO_EMAIL_BACKEND to the console backend.
EMAIL_BACKEND = os.environ.get(
    "DJANGO_EMAIL_BACKEND",
    "django.core.mail.backends.smtp.EmailBackend",
)
# A bare address: Elastic Beanstalk's env file is sourced by a shell in the
# deploy hooks, where "<" and ">" would break it. The display name is added here.
DEFAULT_FROM_EMAIL = os.environ.get(
    "DJANGO_DEFAULT_FROM_EMAIL",
    "no-reply@syncmily.local",
)
if "<" not in DEFAULT_FROM_EMAIL:
    DEFAULT_FROM_EMAIL = f"Syncmily <{DEFAULT_FROM_EMAIL}>"

# Only used when DJANGO_EMAIL_BACKEND is the SMTP backend.
EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "true").lower() == "true"
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_TIMEOUT = 10
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# The default cache holds chat counters, upstream-down flags and cached trips. It is
# one local-memory cache per process. HTTP rate limits do not live here (they use the
# RateCounter table), and the cap leaves room for every key a busy day creates.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "OPTIONS": {"MAX_ENTRIES": 10000, "CULL_FREQUENCY": 10},
    },
}

# Errors (syncmily/errors.py): the CSRF failure page is branded and never shows why.
CSRF_FAILURE_VIEW = "syncmily.errors.csrf_failure"

# Content Security Policy. nginx sends the same string (03_https.sh) and a test keeps
# the two equal; the middleware adds it to any response that skips nginx. The primary
# host is the first SITE_DOMAINS entry, else the first allowed host.
_site_domains = [
    d.strip() for d in os.environ.get("SITE_DOMAINS", "").split(",") if d.strip()
]
CSP_PRIMARY_HOST = (_site_domains or ALLOWED_HOSTS or ["localhost"])[0]
CSP_POLICY = (
    "default-src 'none'; script-src 'self'; "
    "style-src 'self' https://fonts.googleapis.com; "
    "style-src-elem 'self' https://fonts.googleapis.com "
    "'sha256-NmBAHKlY279x2mY0mfnOU0anE9pVNdbkCyLPbCcMu6g='; "
    "style-src-attr 'unsafe-inline'; font-src https://fonts.gstatic.com; "
    "img-src 'self' data: blob:; "
    "connect-src 'self' wss://$PRIMARY https://photon.komoot.io; manifest-src 'self'; "
    "form-action 'self' https://accounts.google.com; frame-ancestors 'none'; "
    "base-uri 'self'; object-src 'none'; report-uri /csp-report/; report-to csp"
).replace("$PRIMARY", CSP_PRIMARY_HOST)
# A trial deploy sends the policy as Content-Security-Policy-Report-Only.
CSP_REPORT_ONLY = os.environ.get("CSP_REPORT_ONLY", "").lower() == "true"

# Request size limits: the numbers live in syncmily/limits.py.
DATA_UPLOAD_MAX_MEMORY_SIZE = limits.DATA_UPLOAD_MAX_MEMORY_SIZE
DATA_UPLOAD_MAX_NUMBER_FIELDS = limits.DATA_UPLOAD_MAX_NUMBER_FIELDS
DATA_UPLOAD_MAX_NUMBER_FILES = limits.DATA_UPLOAD_MAX_NUMBER_FILES
FILE_UPLOAD_MAX_MEMORY_SIZE = limits.FILE_UPLOAD_MAX_MEMORY_SIZE

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
# Set this in production to the exact redirect URI registered with Google.
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "")
# Server-side Google Maps Platform key: Places (location search) and Routes
# (leave-by times and travel checks). Empty falls back to the free providers
# (Photon for search, Transitous / open routing for trips).
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")

# Fernet keys for encrypting stored OAuth tokens, newest first.
FIELD_ENCRYPTION_KEYS = [
    key.strip()
    for key in os.environ.get("FIELD_ENCRYPTION_KEYS", "").split(",")
    if key.strip()
]
# Channel layer for local real-time messaging
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    },
}

# Two-step sign-in (accounts/two_step.py): the name shown in authenticator apps.
OTP_TOTP_ISSUER = "Syncmily"
