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


class ContextPeekNode(template.Node):
    """Positive twin of ContextPokeNode: a read-only third-party-style node,
    declared context-safe."""

    dtc_context_safe = True

    def __init__(self, var):
        self.var = template.Variable(var)

    def render(self, context):
        try:
            return f"<{self.var.resolve(context)}>"
        except template.VariableDoesNotExist:
            return ""


@register.tag
def peek(parser, token):
    return ContextPeekNode(token.split_contents()[1])


class SafeAutoescapeOffNode(AutoescapeOffNode):
    """Declared-safe node that flips autoescape — allowed by the contract;
    the bridge's autoescape resync keeps it exact."""

    dtc_context_safe = True


@register.tag
def aoff_safe(parser, token):
    return SafeAutoescapeOffNode()


class SafeWrapNode(template.Node):
    """Declared-safe container: renders its (listed) child nodelist against
    the live context. Children speak for themselves — contract clause (d)."""

    dtc_context_safe = True
    child_nodelists = ("nodelist",)

    def __init__(self, nodelist):
        self.nodelist = nodelist

    def render(self, context):
        return f"[{self.nodelist.render(context)}]"


@register.tag
def safewrap(parser, token):
    nodelist = parser.parse(("endsafewrap",))
    parser.delete_first_token()
    return SafeWrapNode(nodelist)


@register.simple_tag(takes_context=True)
def ctx_reader_safe(context, key):
    return f"[{context.get(key, 'absent')}]"


ctx_reader_safe.dtc_context_safe = True


class StoreNode(template.Node):
    """A capture tag: renders its body and stores the result in a context
    variable whose name is fixed at parse time — the declared-writes case."""

    dtc_context_writes = ("save_to",)
    child_nodelists = ("nodelist",)

    def __init__(self, nodelist, save_to):
        self.nodelist = nodelist
        self.save_to = save_to

    def render(self, context):
        context[self.save_to] = self.nodelist.render(context)
        return ""


@register.tag
def store(parser, token):
    """{% store as name %}...{% endstore %}"""
    pieces = token.split_contents()
    if len(pieces) != 3 or pieces[1] != "as":
        raise template.TemplateSyntaxError("usage: {% store as name %}")
    nodelist = parser.parse(("endstore",))
    parser.delete_first_token()
    return StoreNode(nodelist, pieces[2])


class RememberNode(template.Node):
    """A root-layer writer: persists a value across templates by writing
    context.dicts[0] — the declared-writes case whose write outlives every
    scope pop."""

    dtc_context_writes = ("save_to",)

    def __init__(self, save_from, save_to):
        self.save_from = save_from
        self.save_to = save_to

    def render(self, context):
        try:
            result = self.save_from.resolve(context)
        except template.VariableDoesNotExist:
            result = None
        context.dicts[0][self.save_to] = result
        return ""


@register.tag
def remember(parser, token):
    """{% remember value as name %}"""
    pieces = token.split_contents()
    if len(pieces) != 4 or pieces[2] != "as":
        raise template.TemplateSyntaxError("usage: {% remember value as name %}")
    return RememberNode(parser.compile_filter(pieces[1]), pieces[3])


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
