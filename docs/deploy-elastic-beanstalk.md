# Deploying Syncmily to Elastic Beanstalk

Syncmily runs on the **Syncmily / django-env** Elastic Beanstalk environment in
`us-east-1` (Python on Amazon Linux 2023, behind an application load balancer).

## What the repo configures

- `Procfile` runs Daphne, so both regular pages and the household chat's
  WebSockets work. nginx on the instance forwards to it on port 8000.
- `.ebextensions/01_django.config`:
  - runs `migrate` once per deploy and `collectstatic` on every instance;
  - lets nginx serve `/static/`;
  - points the load balancer health check at `/healthz/`;
  - keeps the environment at one instance.
- `.platform/hooks/postdeploy/01_calendar_sync_timer.sh` installs a systemd
  timer that runs `python manage.py sync_calendars` every 15 minutes.

## Environment properties

Set these in the EB console under **Configuration → Updates, monitoring and
logging → Environment properties**. Never commit their values.

| Name | Value |
|---|---|
| `DJANGO_SECRET_KEY` | A new random secret for production |
| `DJANGO_DEBUG` | `false` |
| `DJANGO_ALLOWED_HOSTS` | The site's host name(s), comma-separated |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | The site's origin(s), e.g. `https://syncmily.example.com` |
| `DJANGO_HTTPS` | `true` once the site is served over HTTPS, otherwise `false` |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT` | The RDS database |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | The Google OAuth client |
| `GOOGLE_REDIRECT_URI` | `https://<site>/calendars/google/callback/`, also registered in Google Cloud |
| `FIELD_ENCRYPTION_KEYS` | The production Fernet key. Losing it means everyone reconnects Google Calendar. |

## Deploy

```
eb deploy django-env
eb health django-env
```

`eb deploy` uploads the last commit, so commit before deploying.

## Notes

- Google only accepts `https://` redirect URIs outside localhost, so Google
  Calendar sign-in needs HTTPS on the live site.
- The RDS security group must allow port 5432 from the EB instances' security
  group only. Don't make the database publicly accessible.
