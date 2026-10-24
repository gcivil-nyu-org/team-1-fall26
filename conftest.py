"""pytest bridge so mutmut can run the Django unittest suite (no pytest-django needed)."""
import os

import django
import pytest


def pytest_configure():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "syncmily.settings")
    django.setup()


@pytest.fixture(scope="session", autouse=True)
def _django_test_db():
    from django.test.runner import DiscoverRunner
    from django.test.utils import setup_test_environment

    try:
        setup_test_environment()
    except RuntimeError:
        pass
    runner = DiscoverRunner(interactive=False, verbosity=0)
    old = runner.setup_databases()
    yield
    runner.teardown_databases(old)
