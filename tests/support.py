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


@register.simple_tag
def stamp(prefix, times=1):
    return f"<{prefix * times}>"


@register.simple_tag(takes_context=True)
def ctx_reader(context, key):
    return f"[{context.get(key, 'absent')}]"


@register.simple_tag(takes_context=True)
def ctx_set(context, key, value):
    context[key] = value
    return ""


@register.simple_tag(takes_context=True)
def ctx_autoescape_off(context):
    context.autoescape = False
    return "<tag>"


@register.simple_tag
def kw_any(**kwargs):
    return ";".join(f"{k}={v}" for k, v in sorted(kwargs.items()))


@register.inclusion_tag("inc_tag.html")
def card(label, value="?"):
    return {"label": label, "value": value}


@register.inclusion_tag("inc_tag.html", takes_context=True)
def card_ctx(context):
    return {"label": "from-ctx", "value": context.get("name", "?")}


@register.inclusion_tag("inc_tag.html", takes_context=True)
def card_autoescape_off(context):
    context.autoescape = False
    return {"label": "<l>", "value": "v"}


@register.inclusion_tag("inc_tag.html", takes_context=True)
def card_forloop(context):
    forloop = context.get("forloop") or {}
    return {"label": "loop", "value": forloop.get("counter", "?")}


class ContextPokeNode(template.Node):
    """A raw third-party-style node: mutates the live context."""

    def __init__(self, var_name):
        self.var_name = var_name

    def render(self, context):
        context[self.var_name] = "poked"
        return "<poke>"


@register.tag
def poke(parser, token):
    return ContextPokeNode(token.split_contents()[1])


class AutoescapeOffNode(template.Node):
    """A raw third-party-style node: flips the live context's autoescape."""

    def render(self, context):
        context.autoescape = False
        return "<aoff>"


@register.tag
def aoff(parser, token):
    return AutoescapeOffNode()


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
