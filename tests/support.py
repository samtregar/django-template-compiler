"""Shared helpers for the test suite.

Also a template tag library (``register``): tests load it via the engine's
``builtins`` or ``libraries`` OPTIONS to exercise custom filters through the
compiled path — one filter per FilterExpression behavior flag.
"""

from pathlib import Path

from django import template
from django.utils.html import conditional_escape
from django.utils.safestring import mark_safe

TEMPLATE_DIR = Path(__file__).parent / "templates"

register = template.Library()


@register.filter
def shout(value):
    return f"{value}!!"


@register.filter(is_safe=True)
def exclaim(value):
    return f"{value}!"


@register.filter(needs_autoescape=True)
def tagwrap(value, autoescape=None):
    escaped = conditional_escape(value) if autoescape else value
    return mark_safe(f"<x>{escaped}</x>")


@register.filter(expects_localtime=True)
def hourof(value):
    return value.hour


@register.filter
def crash(value):
    raise RuntimeError("filter boom")


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
