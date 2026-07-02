import django
from django.conf import settings


def pytest_configure():
    settings.configure(
        DEBUG=True,
        INSTALLED_APPS=[],
        USE_TZ=True,
    )
    django.setup()
