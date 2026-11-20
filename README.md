# Team Project repo

| Branch | Build | Coverage |
|---|---|---|
| main | [![CI](https://github.com/gcivil-nyu-org/team-1-fall26/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/gcivil-nyu-org/team-1-fall26/actions/workflows/ci.yml?query=branch%3Amain) | [![Coverage Status](https://coveralls.io/repos/github/gcivil-nyu-org/team-1-fall26/badge.svg?branch=main)](https://coveralls.io/github/gcivil-nyu-org/team-1-fall26?branch=main) |
| develop | [![CI](https://github.com/gcivil-nyu-org/team-1-fall26/actions/workflows/ci.yml/badge.svg?branch=develop&event=push)](https://github.com/gcivil-nyu-org/team-1-fall26/actions/workflows/ci.yml?query=branch%3Adevelop+event%3Apush) | [![Coverage Status](https://coveralls.io/repos/github/gcivil-nyu-org/team-1-fall26/badge.svg?branch=develop)](https://coveralls.io/github/gcivil-nyu-org/team-1-fall26?branch=develop) |
| main (daily) | [![CI](https://github.com/gcivil-nyu-org/team-1-fall26/actions/workflows/ci.yml/badge.svg?event=schedule)](https://github.com/gcivil-nyu-org/team-1-fall26/actions/workflows/ci.yml?query=event%3Aschedule) | |

Syncmily, live at https://syncmily.app.

## Checks before you push
GitHub Actions ([`ci.yml`](.github/workflows/ci.yml)) runs these on every pull request, on every push to `develop` and `main`, and daily on `main`:

```
black --check .
flake8 .
python manage.py makemigrations --check --dry-run
coverage run manage.py test && coverage report
```

Install the tools with `pip install --require-hashes -r requirements.txt -r requirements-dev.txt`. Run `black .` to fix formatting.

Settings fail closed: HTTPS flags are on unless `DJANGO_HTTPS=false`, and the email backend is SMTP unless `DJANGO_EMAIL_BACKEND` says otherwise. Set `DJANGO_HTTPS=false` in `.env` for local runs and tests, or every request turns into a redirect. The database connection verifies TLS (`POSTGRES_SSLMODE`, default `verify-full`, against `syncmily/certs/rds-global-bundle.pem`). libpq skips TLS on a Unix socket, so a local server reached through `POSTGRES_HOST=/tmp` works as is. For a local server over TCP, set `POSTGRES_SSLMODE=disable`.

A green push to `main` deploys to production (Elastic Beanstalk `syncmily-env`). A green push to `develop` deploys to the integration environment once the `EB_INTEGRATION_ENV` repository variable names one. The deploy job signs in to AWS with GitHub's OIDC token, so no AWS keys live in GitHub.
