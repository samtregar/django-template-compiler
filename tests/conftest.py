import django
from django.conf import settings


def pytest_configure():
    # DEBUG must be False: engines inherit it, and dtc (by design) refuses
    # to compile for debug engines. test_debug_engine_falls_back opts in
    # explicitly via the engine option.
    settings.configure(
        DEBUG=False,
        INSTALLED_APPS=[],
        USE_TZ=True,
    )
    django.setup()
