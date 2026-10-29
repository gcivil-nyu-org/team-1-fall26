# Team Project repo

| Branch | Build | Coverage |
|---|---|---|
| main | [![Build Status](https://app.travis-ci.com/gcivil-nyu-org/team-1-fall26.svg?branch=main)](https://app.travis-ci.com/gcivil-nyu-org/team-1-fall26) | [![Coverage Status](https://coveralls.io/repos/github/gcivil-nyu-org/team-1-fall26/badge.svg?branch=main)](https://coveralls.io/github/gcivil-nyu-org/team-1-fall26?branch=main) |
| develop | [![Build Status](https://app.travis-ci.com/gcivil-nyu-org/team-1-fall26.svg?branch=develop)](https://app.travis-ci.com/gcivil-nyu-org/team-1-fall26) | [![Coverage Status](https://coveralls.io/repos/github/gcivil-nyu-org/team-1-fall26/badge.svg?branch=develop)](https://coveralls.io/github/gcivil-nyu-org/team-1-fall26?branch=develop) |

Syncmily, live at https://syncmily.app.

## Checks before you push
Travis runs these on every pull request and on every push to `develop` and `main`:

```
black --check .
flake8 .
coverage run manage.py test && coverage report
```

Install the tools with `pip install -r requirements-dev.txt`. Run `black .` to fix formatting.
