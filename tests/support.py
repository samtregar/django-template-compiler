"""Shared helpers for the test suite."""

from pathlib import Path

TEMPLATE_DIR = Path(__file__).parent / "templates"


def make_backend(cls, **options):
    return cls(
        {
            "NAME": "test",
            "DIRS": [str(TEMPLATE_DIR)],
            "APP_DIRS": False,
            "OPTIONS": options,
        }
    )


def sample_processor(request):
    return {"cp_var": "from processor"}
