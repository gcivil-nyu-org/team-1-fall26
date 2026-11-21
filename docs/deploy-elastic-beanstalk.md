# Deploying Syncmily to Elastic Beanstalk

Syncmily runs on the **Syncmily / syncmily-env** Elastic Beanstalk environment in
`us-east-1`: a single instance (Python 3.14 on Amazon Linux 2023, no load
balancer) with an RDS Postgres database, `syncmily-db`. nginx on the instance
terminates HTTPS with a Let's Encrypt certificate (see `03_https.sh`).

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
- `.platform/hooks/postdeploy/04_purge_timer.sh` installs a nightly systemd
  timer that runs `clearsessions`, `axes_reset_logs --age 30` and
  `purge_expired` (expired rate counters, chat older than 12 months, stale
  invites, used or expired email confirmations).

## Environment properties

Set these in the EB console under **Configuration → Updates, monitoring and
logging → Environment properties**. Never commit their values.

| Name | Value |
|---|---|
| `DJANGO_SECRET_KEY` | A new random secret for production |
| `DJANGO_DEBUG` | `false` |
| `DJANGO_ALLOWED_HOSTS` | The site's host name(s), comma-separated |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | The site's origin(s), e.g. `https://syncmily.example.com` |
| `DJANGO_HTTPS` | `true` (the default when unset). Set `false` only for a plain-HTTP test environment. |
| `DJANGO_EMAIL_BACKEND` | Leave unset for SMTP. The deploy check fails on the console backend. |
| `CSP_REPORT_ONLY` | Optional. `true` sends the Content-Security-Policy as report-only, for a trial deploy. Unset to enforce it. |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT` | The RDS database. Do not set `POSTGRES_SSLMODE`: the default `verify-full` checks the RDS certificate against `syncmily/certs/rds-global-bundle.pem`. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | The Google OAuth client |
| `GOOGLE_REDIRECT_URI` | `https://<site>/calendars/google/callback/`, also registered in Google Cloud |
| `FIELD_ENCRYPTION_KEYS` | The production Fernet key. Losing it means everyone reconnects Google Calendar. |

## Deploy

Deploy from a clean checkout of `develop` after its CI passes. When the deploy
includes migrations, snapshot the database first. The GitHub deploy job does this on its own
for `main` (`pre-deploy-<sha>-<run>`), so its role needs `rds:CreateDBSnapshot` and
`rds:DescribeDBSnapshots`. A manual deploy takes the snapshot by hand:

```
aws rds create-db-snapshot --db-instance-identifier syncmily-db \
    --db-snapshot-identifier syncmily-db-pre-<label>
eb deploy syncmily-env --label <label>
eb health syncmily-env
```

The database CHECK constraints come in three migrations per app: clean the
rows, add the checks as `NOT VALID`, then validate them. Before a deploy that
adds checks, run `python manage.py audit_constraints` against production (it
only reads) to see how many rows the cleaning step will change. Plans outside
1900-2100 or longer than 90 days are moved, with one log line each.

`eb deploy` uploads the last commit, so commit before deploying. Use a label
that names the PR and commit, such as `pr120-f35495e`. The AWS profile comes
from `.elasticbeanstalk/config.yml`, or pass `--profile`.

To roll back, fix forward when you can. Redeploying an older label isn't
enough after a migration that adds a required column, because the old code
doesn't fill it in: migrate the app back first, or restore the snapshot.

## Dependencies

`requirements.in` lists the packages Syncmily uses directly. `requirements.txt`
is generated from it: every package, including dependencies of dependencies,
pinned to an exact version with hashes, so the server installs exactly what was
tested and audited. Don't edit `requirements.txt` by hand. After changing
`requirements.in`, regenerate it and commit both files:

```
uv pip compile requirements.in --universal --python-version 3.14 --generate-hashes -o requirements.txt
```

To pick up security fixes in dependencies, run the same command with
`--upgrade`, then run the tests.

## Notes

- Google only accepts `https://` redirect URIs outside localhost, so Google
  Calendar sign-in needs HTTPS on the live site.
- The RDS security group must allow port 5432 from the EB instances' security
  group only. Don't make the database publicly accessible.

## Security settings

- **Admin path.** Set `DJANGO_ADMIN_PATH` to a long random value (for example
  `python -c "import secrets; print(secrets.token_urlsafe(12))"`). The admin then
  lives at `/<that value>/`, and `/admin/` returns 404. Share the path with the
  team privately, not in the repo.
- **Login lockout.** django-axes locks a username for 30 minutes from one IP after
  5 wrong passwords, on the app login and the admin. To unlock someone early, run
  `python manage.py axes_reset_username <username>` on the instance (see the
  `eb ssh` notes above).
- **Scanner traffic.** `.platform/nginx/conf.d/elasticbeanstalk/01_block_probes.conf`
  drops requests for PHP files, WordPress paths and dotfiles before they reach
  Django.
