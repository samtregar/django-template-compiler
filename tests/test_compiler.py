"""Differential tests for the phase 1 compiler (text + variables).

Every case renders through both dtc and Django's stock backend and must
produce identical output. Cases marked compiled also assert that dtc
actually took the compiled path — otherwise these tests would silently
compare Django with Django.
"""

import datetime

import pytest
from django.template import TemplateDoesNotExist
from django.template.backends.django import DjangoTemplates
from django.test import RequestFactory
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy

from dtc.backend import DTCTemplates

from support import make_backend


# --- exotic context objects -------------------------------------------------


class Product:
    def __init__(self):
        self.name = "Widget<1>"

    def get_label(self):
        return "<label>"

    def delete(self):
        raise AssertionError("alters_data callable must never be called")

    delete.alters_data = True

    @property
    def loud(self):
        return self.name.upper()

    @property
    def broken(self):
        raise ValueError("boom")

    @property
    def gone(self):
        raise AttributeError("simulated missing")


class DoNotCall:
    do_not_call_in_templates = True

    def __call__(self):
        raise AssertionError("must not be called")

    def __str__(self):
        return "<dnc>"


class NeedsArgs:
    def __call__(self, required):
        return required


class HtmlObj:
    def __html__(self):
        return "<i>already safe</i>"

    def __str__(self):
        return "plain str form"


class SubStr(str):
    pass


class NoLen:
    """Iterable without __len__: ForNode materializes it with list()."""

    def __iter__(self):
        return iter(["g1", "g2", "g3"])


class SilentException(Exception):
    silent_variable_failure = True


class Silent:
    def __getitem__(self, key):
        raise SilentException

    @property
    def prop(self):
        raise SilentException


def base_context():
    return {
        "name": "world",
        "html": "<b>&\"unsafe'</b>",
        "safe": mark_safe("<b>presafe</b>"),
        "sub": SubStr("<u>sub</u>"),
        "obj": Product(),
        "d": {"key": "value", "nested": {"deeper": {"end": "bottom"}}},
        "items": ["zero", "one", "two"],
        "n": 42,
        "f": 1234.5678,
        "b": True,
        "none": None,
        "lazy": gettext_lazy("hello"),
        "dt": datetime.datetime(2026, 7, 2, 12, 30, tzinfo=datetime.timezone.utc),
        "htmlobj": HtmlObj(),
        "dnc": DoNotCall(),
        "needs_args": NeedsArgs(),
        "fn": lambda: "<called>",
        "silent": Silent(),
        "b": "truthy",
        "empty_list": [],
        "pairs": [("a", 1), ("b", 2)],
        "gen": NoLen(),
        "repeats": ["a", "a", "b", "b", "b", "c"],
        "prefix": "<pre>",
        "people": [
            {"name": "ann", "team": "red"},
            {"name": "bob", "team": "red"},
            {"name": "cat", "team": "blue"},
        ],
        "tzname": "Asia/Tokyo",
        "digitmap": {"0": "string-key-zero", "1": "string-key-one"},
    }


# --- machinery ---------------------------------------------------------------


def render_both(source, context=None, **options):
    """Render through dtc and stock Django; return (dtc_template, dtc_out, django_out)."""
    options.setdefault("builtins", ["support"])  # custom filters, no {% load %}
    dtc_template = make_backend(DTCTemplates, **options).from_string(source)
    django_template = make_backend(DjangoTemplates, **options).from_string(source)
    context = base_context() if context is None else context
    return dtc_template, dtc_template.render(dict(context)), django_template.render(dict(context))


def assert_identical_and_compiled(source, context=None, **options):
    template, actual, expected = render_both(source, context, **options)
    assert actual == expected
    assert template._compiled is not None, f"expected compiled path for {source!r}"


# --- differential cases ------------------------------------------------------

CASES = [
    # plain text / empty
    "",
    "just text, no variables — ünïcödé ok",
    # simple lookups and escaping
    "Hello {{ name }}!",
    "{{ html }}",
    "{{ safe }}",
    "{{ sub }}",
    # dotted lookups: attr, dict, index, method, property
    "{{ obj.name }}",
    "{{ d.key }}",
    "{{ items.1 }}",
    "{{ d.items }}",  # dict has no 'items' key -> bound method, called
    "{{ obj.get_label }}",
    "{{ obj.loud }}",
    "{{ d.nested.deeper.end }}",
    # callables with special markers
    "{{ obj.delete }}",  # alters_data -> string_if_invalid
    "{{ dnc }}",  # do_not_call_in_templates -> str() of instance
    "{{ needs_args }}",  # requires args -> string_if_invalid
    "{{ fn }}",  # plain callable -> called, result escaped
    # missing variables
    "[{{ missing }}]",
    "[{{ missing.deep.er }}]",
    "[{{ d.absent }}]",
    "[{{ items.9 }}]",
    # exceptions flagged silent_variable_failure -> string_if_invalid
    "[{{ silent.anything }}]",
    "[{{ silent.prop }}]",
    # non-string values
    "{{ n }} {{ f }} {{ b }} {{ none }}",
    "{{ dt }}",
    "{{ lazy }}",
    "{{ htmlobj }}",
    # literals
    '{{ "quoted<>" }} {{ 2.5 }} {{ 37 }} {{ True }} {{ None }}',
    # filters: builtins across the behavior flags
    "{{ name|upper }} {{ html|title }}",
    "{{ missing|default:'fallback' }}",
    "{{ none|default:'nada' }}",
    "{{ html|safe }}",
    "{{ html|escape }} {{ safe|escape }}",
    "{{ items|join:', ' }} {{ items|join:html }}",  # needs_autoescape + var arg
    "{{ dt|date:'Y-m-d H:i' }} {{ dt|time }}",  # expects_localtime
    "{{ n|add:8 }} {{ n|add:'7' }} {{ n|add:n }}",
    "{{ f|floatformat:2 }} {{ f|floatformat:'-1' }}",
    "{{ html|striptags|upper }}",
    "{{ name|slice:':3'|capfirst }}",
    "{{ items|length }}",
    "{{ html|truncatechars:8 }} {{ html|truncatewords:1 }}",
    # filters: safety propagation through chains
    "{{ safe|upper }}",  # SafeString in, is_safe=False filter -> re-escaped
    "{{ safe|slice:':4' }}",  # is_safe=True filter keeps input's safety
    "{{ html|upper|lower }}",
    # custom filters from the support library (compile natively)
    "{{ name|shout }} {{ html|shout }} {{ safe|shout }}",
    "{{ html|exclaim }} {{ safe|exclaim }}",  # is_safe=True
    "{{ html|tagwrap }} {{ safe|tagwrap }}",  # needs_autoescape
    "{{ dt|hourof }}",  # expects_localtime
    "{{ name|shout|exclaim|tagwrap }}",
    # filters on literals (never fold through a filter call)
    '{{ "abc<"|upper }} {{ 2.5|add:1 }} {{ 40|add:"2" }}',
    # everything at once
    "<p>{{ name }} bought {{ obj.name }} for {{ f }} at {{ d.key }}</p>",
    # {% if %}: bare variables, operators, precedence, errors-mean-False
    "{% if name %}yes{% endif %}",
    "{% if missing %}yes{% else %}no{% endif %}",
    "{% if none %}a{% elif b %}b{% elif missing %}c{% else %}d{% endif %}",
    "{% if n == 42 %}eq{% endif %} {% if n != 42 %}ne{% endif %}",
    "{% if n > 41 and name %}both{% endif %}",
    "{% if missing or name %}or{% endif %}",
    "{% if not missing %}not{% endif %}",
    "{% if 'o' in name %}in{% endif %} {% if 'q' not in name %}notin{% endif %}",
    "{% if n in name %}weird{% else %}type-error-is-false{% endif %}",
    "{% if name|length > 3 %}long{% endif %}",
    "{% if obj.name %}attr{% endif %} {% if d.absent %}x{% else %}absent{% endif %}",
    "{% if obj.delete %}callable-sii{% else %}falsy{% endif %}",
    # {% for %}: forloop variants, empty, reversed, unpacking, nesting
    "{% for x in items %}{{ x }},{% endfor %}",
    "{% for x in items %}{{ forloop.counter }}:{{ x }} {% endfor %}",
    "{% for x in items %}{{ forloop.counter0 }}{{ forloop.revcounter }}"
    "{{ forloop.revcounter0 }}{{ forloop.first }}{{ forloop.last }} {% endfor %}",
    "{% for x in items reversed %}{{ x }}{% endfor %}",
    "{% for x in missing %}{{ x }}{% empty %}nothing{% endfor %}",
    "{% for x in empty_list %}{{ x }}{% empty %}empty!{% endfor %}",
    "{% for k, v in d.nested.items %}{{ k }}={{ v }};{% endfor %}",
    "{% for a, b in pairs %}{{ a }}-{{ b }} {% endfor %}",
    "{% for x in items %}{% for y in items %}{{ forloop.counter }}."
    "{{ forloop.parentloop.counter }} {% endfor %}{% endfor %}",
    "{% for x in items %}{{ x }}{% endfor %}{{ x }}",  # loop var scope pops
    "{% for x in name %}{{ x }}.{% endfor %}",  # string iteration
    # {% with %}
    "{% with total=items greeting='hi<' %}{{ greeting }} {{ total.0 }}{% endwith %}{{ total }}",
    "{% with inner=d.nested %}{{ inner.deeper.end }}{% endwith %}",
    "{% with v=missing %}[{{ v }}]{% endwith %}",
    # {% autoescape %}
    "{% autoescape off %}{{ html }} {{ safe }}{% endautoescape %}{{ html }}",
    "{% autoescape on %}{{ html }}{% endautoescape %}",
    "{% autoescape off %}{% autoescape on %}{{ html }}{% endautoescape %}{{ html }}{% endautoescape %}",
    "{% autoescape off %}{{ html|tagwrap }}{% endautoescape %}",  # needs_autoescape sees off
    # {% comment %} / {% verbatim %}
    "a{% comment %}gone {{ name }} {% now 'Y' %}{% endcomment %}b",
    "{% verbatim %}{{ name }} {% if %} raw{% endverbatim %}",
    # combined control flow
    "{% for x in items %}{% if forloop.first %}[{% endif %}{{ x }}"
    "{% if forloop.last %}]{% else %},{% endif %}{% endfor %}",
    "{% for x in items %}{% with double=x %}{{ double }}{% endwith %}{% endfor %}",
    "{% if items %}{% for x in items %}{{ x|upper }}{% endfor %}{% endif %}",
    "{% for x in gen %}{{ x }}{% endfor %}",  # no __len__ -> list()
    # inner loop shadows outer loop var; outer scope restores after pop
    "{% for x in items %}{% for x in pairs %}{{ x.0 }}{% endfor %}{{ x }}|{% endfor %}",
    # phase 5: built-in tags without dedicated codegen bridge per-node
    "a{% now 'Y' %}b{{ name }}",
    "{% for x in items %}{% cycle 'odd' 'even' %}:{{ x }} {% endfor %}",
    "{% for x in repeats %}{% ifchanged x %}new:{{ x }}{% endifchanged %}.{% endfor %}"
    "{% for x in repeats %}{% ifchanged x %}again:{{ x }}{% endifchanged %}.{% endfor %}",
    "{% firstof missing none b 'fallback<' %}",
    "{% firstof missing as fo %}[{{ fo }}]",
    "{% spaceless %}<p> <a>{{ name }}</a> </p>{% endspaceless %}",
    "{% widthratio n 100 10 %}",
    "{% templatetag openblock %}x{% templatetag closevariable %}",
    "{% filter upper|lower %}Mixed {{ name }}{% endfilter %}",
    "{% regroup people by team as teams %}{% for t in teams %}{{ t.grouper }}:"
    "{% for p in t.list %}{{ p.name }},{% endfor %};{% endfor %}",
    "{% lorem 5 w %}",
    # phase 5: custom simple_tags compile natively
    "{% stamp name %} {% stamp name 2 %} {% stamp prefix=html times=2 %}",
    "{% stamp name|upper %}",  # filtered argument
    "{% stamp 'const' 3 %}",  # foldable arguments
    "{% stamp name as st %}[{{ st }}]",  # target_var
    "{% ctx_reader 'name' %} {% ctx_reader 'missing' %}",
    "{% kw_any b=2 a=name class='x' %}",  # keyword-named kwarg via **kwargs
    "{% for x in items %}{% ctx_reader 'forloop' %}{% endfor %}",  # forces forloop
    # phase 5: raw third-party node mutating the live context
    "{% poke stamped %}{{ stamped }}",
    "{% for x in items %}{% poke inner %}{{ inner }}{% endfor %}",
    # phase 6: container tags compile (bodies no longer render interpreted)
    "{% spaceless %}<p> <a>{% for x in items %} <i>{{ x }}</i> {% endfor %}</a> </p>{% endspaceless %}",
    "{% filter upper %}mixed {{ name }} and {{ html }}{% endfilter %}",
    "{% filter truncatechars:12|lower %}LONG {{ name }} OUTPUT HERE{% endfilter %}",
    "{% for x in repeats %}{% ifchanged %}{{ x }}{% else %}-{% endifchanged %}{% endfor %}",
    "{% for p in people %}{% ifchanged p.team %}[{{ p.team }}]{% else %}.{% endifchanged %}"
    "{{ p.name }}{% endfor %}",
    "{% for x in items %}{% for y in repeats %}{% ifchanged %}{{ y }}{% endifchanged %}"
    "{% endfor %}|{% endfor %}",  # inner-loop state resets per outer iteration
    "{% ifchanged name %}outside-loop{% endifchanged %}",
    "{% load l10n %}{% localize off %}{{ f }}{% endlocalize %} {{ f }}"
    "{% localize on %}{{ f }}{% endlocalize %}",
    "{% load tz %}{% localtime off %}{{ dt }}{% endlocaltime %} {{ dt }}",
    "{% load tz %}{% timezone 'America/New_York' %}{{ dt }}{% endtimezone %}"
    "{% timezone tzname %}{{ dt }}{% endtimezone %}",
    "{% load i18n %}{% language 'de' %}{{ name }}{% endlanguage %}",
    # digit-bit lookups: sequences, string-digit dict keys, out of range
    "{{ pairs.0.1 }} {{ pairs.1.0 }} {{ items.2 }}",
    "{{ digitmap.0 }} {{ digitmap.1 }}",
    "[{{ pairs.9 }}] [{{ digitmap.7 }}]",
    # phase 8 scope locals: rebinding through non-opaque and opaque tags
    "{% for x in items %}{% stamp 'p' as x %}{{ x }}|{% endfor %}",  # known rebind
    "{% for x in items %}{% poke x %}{{ x }}|{% endfor %}",  # opaque rebind: no locals
    "{% for x in items %}{% with x=name %}{{ x }}{% endwith %}[{{ x }}]{% endfor %}",
    "{% with n=f %}{% for n in items %}{{ n }}{% endfor %}{{ n }}{% endwith %}",
]


@pytest.mark.parametrize("source", CASES)
def test_differential_compiled(source):
    assert_identical_and_compiled(source)


@pytest.mark.parametrize("source", CASES)
def test_differential_autoescape_off(source):
    assert_identical_and_compiled(source, autoescape=False)


@pytest.mark.parametrize("string_if_invalid", ["", "INVALID", "INVALID:%s:"])
@pytest.mark.parametrize(
    "source",
    ["[{{ missing }}]", "[{{ missing.deep }}]", "{{ obj.delete }}", "{{ needs_args }}"],
)
def test_differential_string_if_invalid(source, string_if_invalid):
    assert_identical_and_compiled(source, string_if_invalid=string_if_invalid)


def test_exception_propagates_like_django():
    for backend_cls in (DTCTemplates, DjangoTemplates):
        template = make_backend(backend_cls).from_string("{{ obj.broken }}")
        with pytest.raises(ValueError, match="boom"):
            template.render(base_context())


def test_property_raising_attributeerror_reraised():
    # bit in dir(current) -> Django re-raises rather than silencing
    for backend_cls in (DTCTemplates, DjangoTemplates):
        template = make_backend(backend_cls).from_string("{{ obj.gone }}")
        with pytest.raises(AttributeError, match="simulated missing"):
            template.render(base_context())


def test_context_processors_run():
    options = {"context_processors": ["support.sample_processor"]}
    request = RequestFactory().get("/")
    outputs = []
    for backend_cls in (DTCTemplates, DjangoTemplates):
        template = make_backend(backend_cls, **options).from_string("{{ cp_var }}")
        outputs.append(template.render(request=request))
    assert outputs[0] == outputs[1] == "from processor"


def test_render_reusable():
    template = make_backend(DTCTemplates).from_string("{{ name }}")
    assert template._compiled is not None
    assert template.render({"name": "a"}) == "a"
    assert template.render({"name": "b"}) == "b"


# --- inheritance and inclusion (phase 4) --------------------------------------


def render_named_both(templates, name, context=None, **options):
    """Render template *name* from a locmem set through both backends."""
    options.setdefault("builtins", ["support"])
    options["loaders"] = [("django.template.loaders.locmem.Loader", dict(templates))]
    context = base_context() if context is None else context
    dtc_template = make_backend(DTCTemplates, **options).get_template(name)
    django_out = (
        make_backend(DjangoTemplates, **options).get_template(name).render(dict(context))
    )
    dtc_out = dtc_template.render(dict(context))
    assert dtc_out == django_out
    return dtc_template, dtc_out


INHERITANCE_TEMPLATES = {
    "base.html": (
        "<title>{% block title %}Default{% endblock %}</title>"
        "<body>{% block content %}base:{{ name }}{% endblock %}</body>"
    ),
    "mid.html": (
        "{% extends 'base.html' %}"
        "{% block title %}Mid|{{ block.super }}{% endblock %}"
    ),
    "leaf.html": (
        "{% extends 'mid.html' %}"
        "{% block title %}Leaf|{{ block.super }}{% endblock %}"
        "{% block content %}leaf:{{ html }}{% endblock %}"
    ),
    "leaf_var.html": (
        "{% extends parent %}{% block content %}var-extends{% endblock %}"
    ),
    "base_loop.html": (
        "{% for x in items %}{% block row %}[{{ x }}]{% endblock %}{% endfor %}"
    ),
    "child_forloop.html": (
        "{% extends 'base_loop.html' %}"
        "{% block row %}<{{ forloop.counter }}:{{ x }}>{% endblock %}"
    ),
    "base_now.html": (
        "{% now 'Y' %}{% block content %}base{% endblock %}"
    ),
    "child_of_now.html": (
        "{% extends 'base_now.html' %}"
        "{% block content %}compiled child of interpreted parent{% endblock %}"
    ),
    "child_now.html": (
        "{% extends 'base.html' %}"
        "{% block content %}{% now 'Y' %} interpreted body{% endblock %}"
    ),
    "nested_blocks.html": (
        "{% block outer %}o[{% block inner %}i:{{ name }}{% endblock %}]o{% endblock %}"
    ),
    "override_inner.html": (
        "{% extends 'nested_blocks.html' %}"
        "{% block inner %}override{% endblock %}"
    ),
    "inc.html": "inc:{{ name }}/{{ extra }};",
    "inc_tag.html": "<card>{{ label }}={{ value }}|csrf:{{ csrf_token }}</card>",
    "ifchanged_inc.html": "{% ifchanged x %}{{ x }}{% endifchanged %}",
    "main_ifchanged_state.html": (
        # Two IncludeNodes: each loads its own parse of ifchanged_inc.html,
        # whose IfChangedNode state must stay independent (Django #27974).
        "{% for x in numbers %}{% include 'ifchanged_inc.html' %}"
        "{% include 'ifchanged_inc.html' %}{% endfor %}"
    ),
    "main_cards.html": (
        "{% card 'a' %}{% card name value=html %}{% card_ctx %}"
        "{% for x in items %}{% card x value=forloop.counter %}{% endfor %}"
    ),
    "inc_forloop.html": "({{ forloop.counter }}:{{ x }})",
    "main_inc.html": "{% include 'inc.html' %}",
    "main_inc_with.html": "{% include 'inc.html' with extra='E<' %}",
    "main_inc_only.html": "{% include 'inc.html' with extra=name only %}",
    "main_inc_var.html": "{% include which %}",
    "main_inc_loop.html": "{% for x in items %}{% include 'inc_forloop.html' %}{% endfor %}",
    "main_inc_rel.html": "{% include './inc.html' with extra='rel' %}",
    "inc_chain_mid.html": "mid[{% include 'inc.html' %}]",
    "main_inc_chain.html": "chain{% include 'inc_chain_mid.html' %}",
    "inc_extends.html": (
        "{% extends 'base.html' %}{% block content %}included-child:{{ name }}{% endblock %}"
    ),
    "main_inc_extends.html": "[{% include 'inc_extends.html' %}]",
    "main_inc_only_loop.html": (
        # only + forloop via extra_context: the isolated body can't see the
        # loop, but the value resolved outside it can.
        "{% for x in items %}{% include 'inc.html' with extra=forloop.counter only %}{% endfor %}"
    ),
    "main_inc_only_noforloop.html": (
        # Isolated include in a loop: the target's forloop reads resolve
        # against the new context (empty), so the loop may elide forloop.
        "{% for x in items %}{% include 'inc_forloop.html' only %}{% endfor %}"
    ),
    "tree.html": (
        # Recursive literal include, terminated by the data.
        "{{ node.val }}{% if node.child %}[{% include 'tree.html' with node=node.child only %}]{% endif %}"
    ),
    "main_inc_autoescape.html": (
        "{% autoescape off %}{% include 'inc.html' with extra=html %}{% endautoescape %}"
        "{% include 'inc.html' with extra=html %}"
    ),
}


@pytest.mark.parametrize(
    "name",
    [
        "base.html",
        "mid.html",
        "leaf.html",
        "base_loop.html",
        "child_forloop.html",
        "child_of_now.html",
        "child_now.html",
        "nested_blocks.html",
        "override_inner.html",
        "main_inc.html",
        "main_inc_with.html",
        "main_inc_only.html",
        "main_inc_loop.html",
        "main_inc_rel.html",
        "main_inc_chain.html",
        "main_inc_extends.html",
        "main_inc_only_loop.html",
        "main_inc_only_noforloop.html",
        "main_inc_autoescape.html",
        "main_cards.html",
    ],
)
def test_differential_inheritance(name):
    template, _ = render_named_both(INHERITANCE_TEMPLATES, name)
    assert template._compiled is not None


def test_differential_extends_variable():
    context = dict(base_context(), parent="base.html")
    template, out = render_named_both(
        INHERITANCE_TEMPLATES, "leaf_var.html", context
    )
    assert template._compiled is not None
    assert "var-extends" in out


def test_differential_include_variable():
    context = dict(base_context(), which="inc.html")
    render_named_both(INHERITANCE_TEMPLATES, "main_inc_var.html", context)


def test_include_missing_template():
    for backend_cls in (DTCTemplates, DjangoTemplates):
        backend = make_backend(
            backend_cls,
            loaders=[
                ("django.template.loaders.locmem.Loader", INHERITANCE_TEMPLATES)
            ],
        )
        template = backend.from_string("{% include 'no-such.html' %}")
        with pytest.raises(TemplateDoesNotExist):
            template.render(base_context())


def test_literal_include_takes_fast_path():
    """A literal name compiles to the specialized include site; a variable
    name keeps the generic runtime mirror."""
    options = {
        "loaders": [("django.template.loaders.locmem.Loader", INHERITANCE_TEMPLATES)]
    }
    backend = make_backend(DTCTemplates, **options)
    literal = backend.get_template("main_inc.html")
    assert "_resolve_include(" in literal._compiled.__dtc_source__
    variable = backend.get_template("main_inc_var.html")
    assert "_resolve_include(" not in variable._compiled.__dtc_source__


def test_isolated_include_in_loop_elides_forloop():
    """{% include ... only %} renders against context.new(), which provably
    can't see forloop — the loop skips forloop maintenance. (The target of
    main_inc_only_noforloop.html *does* reference forloop; the differential
    case above proves it resolves empty under both engines.)"""
    options = {
        "loaders": [("django.template.loaders.locmem.Loader", INHERITANCE_TEMPLATES)]
    }
    backend = make_backend(DTCTemplates, **options)
    elided = backend.get_template("main_inc_only_noforloop.html")
    assert "'parentloop'" not in elided._compiled.__dtc_source__
    # extra_context values resolve in the outer context: forloop stays.
    kept = backend.get_template("main_inc_only_loop.html")
    assert "'parentloop'" in kept._compiled.__dtc_source__
    # Non-isolated includes keep forcing forloop: the target sees the
    # outer context, and IfChangedNode state lives on the forloop dict.
    kept = backend.get_template("main_inc_loop.html")
    assert "'parentloop'" in kept._compiled.__dtc_source__


def test_differential_recursive_include():
    context = dict(
        base_context(),
        node={"val": "a", "child": {"val": "b", "child": {"val": "c", "child": None}}},
    )
    template, out = render_named_both(INHERITANCE_TEMPLATES, "tree.html", context)
    assert template._compiled is not None
    assert out == "a[b[c]]"


def test_literal_include_honors_patched_render(monkeypatch):
    """A Template._render patch installed at render time (test
    instrumentation, third-party hooks) must route the include through the
    patched machinery, not around it."""
    from django.template.base import Template as BaseTemplate
    from django.template.context import make_context

    options = {
        "loaders": [("django.template.loaders.locmem.Loader", INHERITANCE_TEMPLATES)]
    }
    backend = make_backend(DTCTemplates, **options)
    template = backend.get_template("main_inc.html")
    expected = make_backend(DjangoTemplates, **options).get_template(
        "main_inc.html"
    ).render(base_context())

    rendered = []
    original = BaseTemplate._render

    def instrumented(self, context):
        rendered.append(self.origin.template_name)
        return original(self, context)

    monkeypatch.setattr(BaseTemplate, "_render", instrumented)
    # Drive the compiled function directly (the top-level entry points
    # would themselves detect the patch and fall back before reaching it).
    base = template.template
    context = make_context(base_context(), autoescape=True)
    with context.render_context.push_state(base):
        with context.bind_template(base):
            out = template._compiled(context)
    assert out == expected
    assert "inc.html" in rendered  # the target rendered through the patch
    """base_loop's own body never references forloop, but a child override
    can — a block inside a loop must force forloop maintenance."""
    options = {
        "loaders": [("django.template.loaders.locmem.Loader", INHERITANCE_TEMPLATES)]
    }
    template = make_backend(DTCTemplates, **options).get_template("base_loop.html")
    assert "'parentloop'" in template._compiled.__dtc_source__


def test_inclusion_tag_csrf_copied():
    """InclusionNode copies csrf_token into the isolated context."""
    context = dict(base_context(), csrf_token="fixed-token-value")
    _, out = render_named_both(INHERITANCE_TEMPLATES, "main_cards.html", context)
    assert "csrf:fixed-token-value" in out


def test_ifchanged_state_independent_across_includes():
    """Regression (caught by Django's suite): sharing a compiled fn across
    same-source template instances aliased their bridged stateful nodes."""
    context = dict(base_context(), numbers=[1, 2, 3])
    _, out = render_named_both(
        INHERITANCE_TEMPLATES, "main_ifchanged_state.html", context
    )
    assert out == "112233"


def test_bridged_templates_not_shared():
    options = {
        "loaders": [("django.template.loaders.locmem.Loader", INHERITANCE_TEMPLATES)]
    }
    backend = make_backend(DTCTemplates, **options)
    first = backend.get_template("ifchanged_inc.html")
    second = backend.get_template("ifchanged_inc.html")
    assert first._compiled is not second._compiled  # per-parse: embeds state
    assert not first._compiled.__dtc_shareable__


def test_source_cache_reuses_compiled_fn():
    """Without a cached loader every get_template returns a fresh Template
    instance; the source cache must prevent recompiling each one."""
    options = {
        "loaders": [("django.template.loaders.locmem.Loader", INHERITANCE_TEMPLATES)]
    }
    backend = make_backend(DTCTemplates, **options)
    first = backend.get_template("leaf.html")
    second = backend.get_template("leaf.html")
    assert first.template is not second.template  # uncached loader: new parse
    assert first._compiled is second._compiled  # same compiled function
    assert second.render(base_context()) == first.render(base_context())


def test_block_bodies_attached():
    options = {
        "loaders": [("django.template.loaders.locmem.Loader", INHERITANCE_TEMPLATES)]
    }
    template = make_backend(DTCTemplates, **options).get_template("leaf.html")
    extends_node = template.template.nodelist[0]
    for block in extends_node.blocks.values():
        assert callable(block.__dict__.get("_dtc_body"))


# --- disk cache ----------------------------------------------------------------


def test_disk_cache_roundtrip(tmp_path):
    from dtc.runtime import stats

    options = {"dtc_disk_cache": str(tmp_path)}
    source = "cache:{{ name }}{% for x in items %}{{ x }}{% endfor %}" + FLAT_MANY

    first = make_backend(DTCTemplates, **options).from_string(source)
    assert first._compiled is not None
    expected = first.render(base_context())
    entries = list(tmp_path.glob("*/*.marshal"))
    assert entries, "compile should have written a cache entry"

    hits = stats["disk_hits"]
    second = make_backend(DTCTemplates, **options).from_string(source)
    assert stats["disk_hits"] == hits + 1  # compile() skipped
    assert second.render(base_context()) == expected


def test_disk_cache_corruption_fails_open(tmp_path):
    options = {"dtc_disk_cache": str(tmp_path)}
    source = "corrupt:{{ name }}" + FLAT_MANY
    make_backend(DTCTemplates, **options).from_string(source)
    for entry in tmp_path.glob("*/*.marshal"):
        entry.write_bytes(b"not marshal data")
    template = make_backend(DTCTemplates, **options).from_string(source)
    assert template._compiled is not None
    assert template.render(base_context())  # recompiled fresh


def test_disk_cache_distinct_sources(tmp_path):
    options = {"dtc_disk_cache": str(tmp_path)}
    make_backend(DTCTemplates, **options).from_string("a:{{ name }}" + FLAT_MANY)
    make_backend(DTCTemplates, **options).from_string("b:{{ name }}" + FLAT_MANY)
    assert len(list(tmp_path.glob("*/*.marshal"))) == 2


# --- tooling compatibility (phase 7) ------------------------------------------


def test_template_rendered_signal_under_test_instrumentation():
    """assertTemplateUsed depends on the template_rendered signal that
    Django's test environment injects by patching Template._render; the
    compiled path must detect the patch and route through it."""
    from django.test.signals import template_rendered
    from django.test.utils import setup_test_environment, teardown_test_environment

    setup_test_environment()
    try:
        received = []

        def receiver(sender, **kwargs):
            received.append(kwargs.get("template"))

        template_rendered.connect(receiver)
        try:
            template = make_backend(DTCTemplates).from_string("sig:{{ name }}")
            assert template.render({"name": "x"}) == "sig:x"
            assert len(received) == 1
        finally:
            template_rendered.disconnect(receiver)
    finally:
        teardown_test_environment()


@pytest.mark.parametrize(
    "source",
    [
        "{% if %}x{% endif %}",
        "{% endif %}",
        "{% unknowntag %}",
        "{{ x|unknownfilter }}",
        "{% for x %}{% endfor %}",
        "{% extends 'a' %}{% extends 'b' %}",
    ],
)
def test_parse_errors_identical(source):
    """Parse-time errors happen in Django's parser before dtc is involved;
    message equality documents the guarantee."""
    from django.template import TemplateSyntaxError

    messages = []
    for backend_cls in (DTCTemplates, DjangoTemplates):
        with pytest.raises(TemplateSyntaxError) as excinfo:
            make_backend(backend_cls).from_string(source)
        messages.append(str(excinfo.value))
    assert messages[0] == messages[1]


def test_compiler_bug_fails_open_and_counts(monkeypatch):
    from dtc import compiler
    from dtc.runtime import stats

    def boom(template):
        raise RuntimeError("injected bug")

    monkeypatch.setattr(compiler, "_compile", boom)
    before = stats["templates_error"]
    template = make_backend(DTCTemplates).from_string("{{ name }}")
    assert template._compiled is None  # fell back
    assert template.render({"name": "x"}) == "x"  # renders via Django
    assert stats["templates_error"] == before + 1  # and is not silent


def test_strict_mode_raises_on_compiler_bug(monkeypatch):
    from dtc import compiler

    def boom(template):
        raise RuntimeError("injected bug")

    monkeypatch.setattr(compiler, "_compile", boom)
    monkeypatch.setattr(compiler, "STRICT", True)
    with pytest.raises(RuntimeError, match="injected bug"):
        make_backend(DTCTemplates).from_string("{{ name }}")


# --- compiled/fallback classification ----------------------------------------


def test_filter_exception_propagates():
    for backend_cls in (DTCTemplates, DjangoTemplates):
        template = make_backend(backend_cls, builtins=["support"]).from_string(
            "{{ name|crash }}"
        )
        with pytest.raises(RuntimeError, match="filter boom"):
            template.render(base_context())


def test_missing_filter_arg_raises():
    # Unlike a missing variable ahead of the filters, a missing variable
    # *argument* raises VariableDoesNotExist in Django.
    from django.template.base import VariableDoesNotExist

    for backend_cls in (DTCTemplates, DjangoTemplates):
        template = make_backend(backend_cls).from_string("{{ name|default:absent }}")
        with pytest.raises(VariableDoesNotExist):
            template.render(base_context())


def test_load_tag_compiles():
    template = make_backend(DTCTemplates, libraries={"custom": "support"}).from_string(
        "{% load custom %}{{ name|shout }}"
    )
    assert template._compiled is not None
    assert template.render({"name": "hi"}) == "hi!!"


def test_unpack_mismatch_error_identical():
    errors = []
    for backend_cls in (DTCTemplates, DjangoTemplates):
        template = make_backend(backend_cls).from_string(
            "{% for a, b in items %}{{ a }}{% endfor %}"
        )
        with pytest.raises(ValueError) as excinfo:
            template.render(base_context())
        errors.append(str(excinfo.value))
    assert errors[0] == errors[1]  # message must match to the character


def test_forloop_elided_when_unused():
    template = make_backend(DTCTemplates).from_string(
        "{% for x in items %}{{ x }}{% endfor %}"
    )
    source = template._compiled.__dtc_source__
    assert "_forloop_" not in source
    assert "enumerate" not in source


def test_forloop_maintained_when_referenced():
    template = make_backend(DTCTemplates).from_string(
        "{% for x in items %}{{ forloop.counter }}{% endfor %}"
    )
    source = template._compiled.__dtc_source__
    assert "'parentloop'" in source
    assert "enumerate" in source


def test_container_tags_fully_compiled():
    """Containers must not bridge — their bodies would render interpreted."""
    for source in (
        "{% spaceless %}{{ a }}{% endspaceless %}",
        "{% filter upper %}{{ a }}{% endfilter %}",
        "{% for x in items %}{% ifchanged %}{{ x }}{% endifchanged %}{% endfor %}",
    ):
        template = make_backend(DTCTemplates).from_string(source)
        assert ".render_annotated(" not in template._compiled.__dtc_source__, source


def test_ifchanged_template_not_shareable():
    template = make_backend(DTCTemplates).from_string(
        "{% ifchanged name %}x{% endifchanged %}"
    )
    assert not template._compiled.__dtc_shareable__


def test_scope_locals_emitted():
    template = make_backend(DTCTemplates).from_string(
        "{% for x in items %}{{ x }}{{ forloop.counter }}{% endfor %}"
    )
    source = template._compiled.__dtc_source__
    assert "_lv0_x = _item_0" in source  # loop var bound to a local
    assert "_value = _lv0_x" in source  # read through the local
    assert "_value = _forloop_0" in source  # forloop read through the local


def test_scope_locals_disabled_by_opaque_bridge():
    """{% poke x %} is an unknown tag that rebinds x: the loop must not
    bind x to a local, or reads after the poke would go stale."""
    template = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% for x in items %}{% poke x %}{{ x }}{% endfor %}"
    )
    source = template._compiled.__dtc_source__
    assert "_lv0_x" not in source
    assert template.render(base_context()) == "<poke>poked" * 3


def test_takes_context_tag_rebinds_scope_names():
    """A takes_context function receives the live context and can write
    loop/with-bound names, exactly like an opaque bridged tag: scope
    locals must be disabled around it."""
    assert_identical_and_compiled(
        "{% for x in items %}{% ctx_set 'x' 'changed' %}{{ x }}|{% endfor %}"
    )
    assert_identical_and_compiled(
        "{% with y=name %}{% ctx_set 'y' 'changed' %}{{ y }}{% endwith %}"
    )
    template = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% for x in items %}{% ctx_set 'x' 'c' %}{{ x }}{% endfor %}"
    )
    assert "_lv" not in template._compiled.__dtc_source__


def test_takes_context_inclusion_tag_reads_forloop():
    """The inclusion *template* renders isolated, but the takes_context
    function itself sees the live context — forloop must be maintained."""
    templates = {
        "inc_tag.html": "<card>{{ label }}={{ value }}</card>",
        "main.html": "{% for x in items %}{% card_forloop %}{% endfor %}",
    }
    render_named_both(templates, "main.html")


def test_autoescape_mutation_by_takes_context_simple_tag():
    """Stock SimpleNode.render reads context.autoescape *after* calling
    the function: a tag that flips it changes its own output's escaping
    and everything after it."""
    assert_identical_and_compiled("{% ctx_autoescape_off %}{{ html }}")
    assert_identical_and_compiled("{% ctx_autoescape_off as t %}{{ t }}{{ html }}")


def test_autoescape_mutation_by_takes_context_inclusion_tag():
    templates = {
        "inc_tag.html": "<card>{{ label }}={{ value }}</card>",
        "main.html": "{% card_autoescape_off %}{{ html }}",
    }
    render_named_both(templates, "main.html")


def test_autoescape_mutation_by_bridged_node():
    assert_identical_and_compiled("{% aoff %}{{ html }}")
    assert_identical_and_compiled(
        "{% autoescape off %}x{% endautoescape %}{% aoff %}{{ html }}"
    )
    # Inside {% autoescape %}: the wrapper's restore still wins afterwards.
    assert_identical_and_compiled(
        "{% autoescape off %}{% aoff %}{{ html }}{% endautoescape %}{{ html }}"
    )
    assert_identical_and_compiled(
        "{% for x in items %}{% aoff %}{{ x }}{% endfor %}{{ html }}"
    )


AUTOESCAPE_LEAK_TEMPLATES = {
    "aoff.html": "{% aoff %}",
    "main_inc.html": "{% include 'aoff.html' %}{{ html }}",
    # Isolated include mutates a context.new() copy: must NOT leak.
    "main_inc_only.html": "{% include 'aoff.html' only %}{{ html }}",
    "main_block.html": "{% block b %}{% aoff %}{% endblock %}{{ html }}",
    "aoff_base.html": "{% block b %}{% aoff %}{% endblock %}{{ html }}",
    "aoff_child.html": (
        "{% extends 'aoff_base.html' %}"
        "{% block b %}[{{ block.super }}]{{ html }}{% endblock %}"
    ),
}


@pytest.mark.parametrize(
    "name", ["main_inc.html", "main_inc_only.html", "main_block.html", "aoff_child.html"]
)
def test_autoescape_mutation_leaks_like_stock(name):
    """Blocks, non-isolated includes, and {{ block.super }} all run foreign
    code against the live context; the hoisted autoescape local must track
    whatever they did — and must NOT see mutations stock wouldn't."""
    render_named_both(AUTOESCAPE_LEAK_TEMPLATES, name)


def test_int_fast_path_respects_thousand_separator():
    from django.test import override_settings

    source = "{{ big }} {% for x in items %}{{ forloop.counter }}{% endfor %}"
    context = dict(base_context(), big=1234567)
    _, plain, _ = render_both(source, context)
    with override_settings(USE_THOUSAND_SEPARATOR=True):
        template, grouped, expected = render_both(source, context)
        assert grouped == expected  # differential under grouping
    assert plain.startswith("1234567")


def test_thousand_separator_override_between_renders():
    """The concrete settings holder is bound per render (never per compile):
    the same compiled template must see override_settings applied and then
    removed across successive renders."""
    from django.test import override_settings

    dtc_template = make_backend(DTCTemplates).from_string("{{ big }}")
    django_template = make_backend(DjangoTemplates).from_string("{{ big }}")
    assert dtc_template._compiled is not None
    context = {"big": 1234567}
    assert dtc_template.render(dict(context)) == django_template.render(dict(context)) == "1234567"
    with override_settings(USE_THOUSAND_SEPARATOR=True):
        grouped = dtc_template.render(dict(context))
        assert grouped == django_template.render(dict(context))
        assert grouped != "1234567", "override was not actually observed"
    assert dtc_template.render(dict(context)) == django_template.render(dict(context)) == "1234567"


def test_thousand_separator_assigned_mid_render_by_bridged_tag():
    """Direct settings assignment from a bridged tag writes through to the
    holder the int fast path reads: the very next {{ int }} must group,
    exactly as stock's live per-value read behaves. (This is the test a
    hoisted per-render *value* would fail — only the holder may be cached.)"""
    from django.test import override_settings

    source = "{{ big }};{% set_thousands on %}{{ big }}"
    context = {"big": 1234567}
    outputs = []
    for backend in (DTCTemplates, DjangoTemplates):
        template = make_backend(backend, builtins=["support"]).from_string(source)
        # each render mutates the setting; give each its own throwaway holder
        with override_settings(USE_THOUSAND_SEPARATOR=False):
            outputs.append(template.render(dict(context)))
        if backend is DTCTemplates:
            assert template._compiled is not None
    dtc_out, django_out = outputs
    assert dtc_out == django_out
    before, after = dtc_out.split(";")
    assert before == "1234567" and after != "1234567", (
        "mid-render setting flip was not observed"
    )


def test_thousand_separator_override_swap_mid_render():
    """override_settings entered by a bridged tag swaps settings._wrapped —
    invisible through a held holder, so the bridge resync must re-grab it."""
    from django.test import override_settings

    source = (
        "{{ big }};{% thousands_override_on %}{{ big }}"
        "{% thousands_override_off %};{{ big }}"
    )
    context = {"big": 1234567}
    outputs = []
    for backend in (DTCTemplates, DjangoTemplates):
        template = make_backend(backend, builtins=["support"]).from_string(source)
        with override_settings(USE_THOUSAND_SEPARATOR=False):
            outputs.append(template.render(dict(context)))
        if backend is DTCTemplates:
            assert template._compiled is not None
    dtc_out, django_out = outputs
    assert dtc_out == django_out
    first, second, third = dtc_out.split(";")
    assert first == third == "1234567"
    assert second != "1234567", "override entered mid-render was not observed"


# --- the escape fast path -----------------------------------------------------

EVERY_ASCII = "".join(map(chr, range(1, 128)))


def test_escape_fast_path_differential():
    context = dict(base_context(), everychar=EVERY_ASCII)
    assert_identical_and_compiled("{{ everychar }}", context)
    assert_identical_and_compiled(
        "{% autoescape off %}{{ everychar }}{% endautoescape %}", context
    )
    assert_identical_and_compiled("{{ everychar|lower }}", context)
    # lazy (Promise) values have a non-str class and must keep taking
    # render_value_in_context, not the inlined escape
    assert_identical_and_compiled("{{ lazy }}{{ html }}")


def test_str_escape_fast_path_selected():
    """On every supported Django, escape() is keep_lazy(SafeString) around
    SafeString(html.escape(str(text))), so the drift guard must accept the
    inlined form. If this fails, Django's escape changed: verify what it
    does now before re-blessing (the compiled output stays correct either
    way — the guard falls back to Django's escape)."""
    from django.utils.html import escape

    from dtc import compiler

    assert compiler._STR_ESCAPE is compiler._fast_str_escape
    assert compiler._pick_str_escape(escape) is compiler._fast_str_escape
    probe = EVERY_ASCII + "\xa0\xe9\u2028\U0001f600"
    assert compiler._fast_str_escape(probe) == escape(probe)
    assert type(compiler._fast_str_escape(probe)) is type(escape(probe))


def test_str_escape_drift_guard_falls_back():
    """Any observable deviation in escape() — different table, different
    return type, missing keep_lazy wrapper, or a raising probe — must make
    the guard bind Django's own escape instead of the inlined form."""
    import html as html_stdlib

    from django.utils.functional import keep_lazy
    from django.utils.safestring import SafeString

    from dtc import compiler

    @keep_lazy(SafeString)
    def drifted_table(text):
        return SafeString(html_stdlib.escape(str(text)).replace("`", "&#96;"))

    @keep_lazy(str)
    def drifted_type(text):
        return html_stdlib.escape(str(text))

    def unwrapped(text):
        return SafeString(html_stdlib.escape(str(text)))

    @keep_lazy(SafeString)
    def broken(text):
        raise RuntimeError("boom")

    for candidate in (drifted_table, drifted_type, unwrapped, broken):
        assert compiler._pick_str_escape(candidate) is candidate


FLAT_MANY = (
    "{{ name }}{{ html }}{{ safe }}{{ n }}{{ f }}{{ missing }}{{ fn }}"
    "{{ obj.name }}{{ d.key }}{{ none }}"
)


def test_flat_snapshot_differential():
    """Templates over the score threshold read through the flat snapshot;
    misses, callables, and dotted tails must stay exact."""
    assert_identical_and_compiled(FLAT_MANY)
    assert_identical_and_compiled(FLAT_MANY, autoescape=False)
    assert_identical_and_compiled(
        "{% for x in items %}{{ name }}:{{ x }} {% endfor %}" + FLAT_MANY
    )
    # a written name read both before and after its write goes via the walk
    assert_identical_and_compiled(
        "[{{ y }}]{% stamp 'v' as y %}[{{ y }}]" + FLAT_MANY
    )


def test_flat_snapshot_emitted_and_gated():
    source_of = lambda t: t._compiled.__dtc_source__
    big = make_backend(DTCTemplates).from_string(FLAT_MANY)
    assert "_flat_get = _flatten_tail(context).get" in source_of(big)
    assert "_flat_get('name'" in source_of(big)

    small = make_backend(DTCTemplates).from_string("{{ name }}")
    assert "_flat_get" not in source_of(small)  # below threshold

    written = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% stamp 'v' as y %}" + "{{ y }}" * 8
    )
    assert "_flat_get('y'" not in source_of(written)  # written name: walks

    opaque = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% poke z %}" + FLAT_MANY
    )
    assert "_flat_get" not in source_of(opaque)  # opaque write: no snapshot

    takes_ctx = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% ctx_reader 'name' %}" + FLAT_MANY
    )
    assert "_flat_get" not in source_of(takes_ctx)  # takes_context: no snapshot


def test_unknown_tags_bridge():
    """Since phase 5, unknown tags compile as per-node bridges instead of
    forcing whole-template fallback."""
    template = make_backend(DTCTemplates).from_string("{% now 'Y' %}")
    assert template._compiled is not None
    assert ".render_annotated(context)" in template._compiled.__dtc_source__


def test_debug_engine_falls_back():
    template = make_backend(DTCTemplates, debug=True).from_string("{{ name }}")
    assert template._compiled is None
    assert template.render({"name": "x"}) == "x"


def test_codegen_shape():
    """White-box: folding and fast paths actually happen."""
    template = make_backend(DTCTemplates).from_string('{{ "lit" }} {{ a.b }}')
    source = template._compiled.__dtc_source__
    assert "_append('lit')" in source  # string literal folded to a constant
    assert "_context_get('a')" in source  # first bit: inline context lookup
    assert "_value['b']" in source  # later bits: inline subscript fast path
    assert "getattr(_value, 'b')" in source  # ... with attribute branch
    import re

    assert re.search(r"_node_\d+\.render\(context\)", source)  # slow-path bridge


def test_codegen_shape_filters():
    template = make_backend(DTCTemplates).from_string("{{ a|join:', '|upper }}")
    source = template._compiled.__dtc_source__
    # join is registered is_safe=True + needs_autoescape=True
    assert "_filter_0_0(_input, _arg_0_0_0, autoescape=_autoescape)" in source
    assert "_filter_0_1(_value)" in source  # upper
    assert "except UnicodeDecodeError:" in source  # VariableNode.render's catch


# --- dtc_context_safe declarations ------------------------------------------


def test_declared_safe_node_differential():
    assert_identical_and_compiled("{% peek name %}")
    assert_identical_and_compiled("{% peek missing %}")
    assert_identical_and_compiled("{% for x in items %}{% peek x %}{% endfor %}")
    assert_identical_and_compiled("{% with y=name %}{% peek y %}{% endwith %}")
    assert_identical_and_compiled("{% peek html %}" + FLAT_MANY)


def test_declared_safe_node_shareable():
    template = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% peek name %}"
    )
    assert template._compiled.__dtc_shareable__

    options = {
        "builtins": ["support"],
        "loaders": [
            (
                "django.template.loaders.locmem.Loader",
                {"peek.html": "{% peek name %}!"},
            )
        ],
    }
    backend = make_backend(DTCTemplates, **options)
    first = backend.get_template("peek.html")
    second = backend.get_template("peek.html")
    assert first.template is not second.template  # uncached loader: new parse
    assert first._compiled is second._compiled  # declared safe: shared
    assert first.render(base_context()) == "<world>!"


def test_declared_safe_keeps_flat_snapshot():
    source_of = lambda t: t._compiled.__dtc_source__
    safe = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% peek z %}" + FLAT_MANY
    )
    assert "_flat_get" in source_of(safe)

    safe_fn = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% ctx_reader_safe 'name' %}" + FLAT_MANY
    )
    assert "_flat_get" in source_of(safe_fn)

    # The undeclared twins stay gated (also covered by
    # test_flat_snapshot_emitted_and_gated).
    plain_fn = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% ctx_reader 'name' %}" + FLAT_MANY
    )
    assert "_flat_get" not in source_of(plain_fn)


def test_scope_locals_survive_declared_safe_bridge():
    template = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% for x in items %}{% peek x %}{{ x }}{% endfor %}"
    )
    source = template._compiled.__dtc_source__
    assert "_lv0_x" in source
    assert template.render(base_context()) == "<zero>zero<one>one<two>two"

    with_tc = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% for x in items %}{% ctx_reader_safe 'x' %}{{ x }}{% endfor %}"
    )
    assert "_lv0_x" in with_tc._compiled.__dtc_source__


def test_declared_safe_still_forces_forloop():
    """v1 declarations don't enumerate reads: a safe tag may resolve
    forloop.counter, so the dict must be maintained."""
    template = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% for x in items %}{% peek forloop.counter %}{% endfor %}"
    )
    assert "'parentloop'" in template._compiled.__dtc_source__
    assert template.render(base_context()) == "<1><2><3>"


def test_safe_container_differential():
    assert_identical_and_compiled("{% safewrap %}a {{ name }} b{% endsafewrap %}")
    assert_identical_and_compiled(
        "{% safewrap %}{% for x in items %}{{ x }}{% endfor %}{% endsafewrap %}"
    )
    template = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% safewrap %}{{ name }}{% endsafewrap %}"
    )
    assert template._compiled.__dtc_shareable__


def test_safe_container_nested_writers():
    """A safe container's children speak for themselves (contract clause d):
    nested writers must still poison flattening, scope locals, and the
    written-name set."""
    source_of = lambda t: t._compiled.__dtc_source__

    nested_opaque = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% safewrap %}{% poke z %}{% endsafewrap %}" + FLAT_MANY
    )
    assert "_flat_get" not in source_of(nested_opaque)
    assert_identical_and_compiled(
        "{% safewrap %}{% poke z %}{% endsafewrap %}{{ z }}" + FLAT_MANY
    )

    in_loop = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% for x in items %}{% safewrap %}{% poke x %}{% endsafewrap %}{{ x }}{% endfor %}"
    )
    assert "_lv" not in source_of(in_loop)
    assert in_loop.render(base_context()) == "[<poke>]poked" * 3

    nested_writer = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% safewrap %}{% stamp 'v' as y %}{% endsafewrap %}{{ y }}" + FLAT_MANY
    )
    assert "_flat_get('name'" in source_of(nested_writer)  # snapshot survives
    assert "_flat_get('y'" not in source_of(nested_writer)  # written name walks
    assert_identical_and_compiled(
        "{% safewrap %}{% stamp 'v' as y %}{% endsafewrap %}{{ y }}" + FLAT_MANY
    )


def test_declared_safe_autoescape_flip():
    """Flipping context.autoescape is outside the contract: the bridge
    resync keeps a declared-safe flipper exact."""
    assert_identical_and_compiled("{% aoff_safe %}{{ html }}")
    assert_identical_and_compiled(
        "{% autoescape off %}{% aoff_safe %}{{ html }}{% endautoescape %}{{ html }}"
    )


def test_check_declarations_catches_lying_node(monkeypatch):
    import dtc
    import support

    monkeypatch.setattr(
        support.ContextPokeNode, "dtc_context_safe", True, raising=False
    )
    monkeypatch.setenv("DTC_CHECK_DECLARATIONS", "1")
    template = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% poke z %}"
    )
    with pytest.raises(dtc.ContextSafeViolation, match="ContextPokeNode"):
        template.render(base_context())


def test_lying_declaration_silent_without_check_mode(monkeypatch):
    import support

    monkeypatch.setattr(
        support.ContextPokeNode, "dtc_context_safe", True, raising=False
    )
    template = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% poke z %}"
    )
    assert template.render(base_context()) == "<poke>"


def test_check_declarations_catches_lying_function(monkeypatch):
    import dtc
    import support

    monkeypatch.setattr(support.ctx_set, "dtc_context_safe", True, raising=False)
    monkeypatch.setenv("DTC_CHECK_DECLARATIONS", "1")
    template = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% ctx_set 'z' 'v' %}"
    )
    with pytest.raises(dtc.ContextSafeViolation, match="ctx_set"):
        template.render(base_context())


def test_check_declarations_passes_honest(monkeypatch):
    monkeypatch.setenv("DTC_CHECK_DECLARATIONS", "1")
    assert_identical_and_compiled("{% peek name %}" + FLAT_MANY)
    assert_identical_and_compiled("{% safewrap %}{{ name }}{% endsafewrap %}")
    assert_identical_and_compiled("{% ctx_reader_safe 'name' %}")
    template = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% peek name %}"
    )
    assert "_checked_safe_render" in template._compiled.__dtc_source__


def test_check_declarations_skips_containers_with_writers(monkeypatch):
    """A safe container wrapping a legitimate writer (contract clause d)
    must bridge unchecked, or the checker would false-positive."""
    monkeypatch.setenv("DTC_CHECK_DECLARATIONS", "1")
    template = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% safewrap %}{% poke z %}{% endsafewrap %}{{ z }}"
    )
    assert "_checked_safe_render" not in template._compiled.__dtc_source__
    assert template.render(base_context()) == "[<poke>]poked"


def test_declare_safe_helper():
    import dtc
    from django import template

    class LocalNode(template.Node):
        def render(self, context):
            return ""

    assert dtc.declare_safe(LocalNode) is LocalNode
    assert LocalNode.dtc_context_safe is True

    def tag_fn(context):
        return ""

    assert dtc.declare_safe(tag_fn) is tag_fn
    assert tag_fn.dtc_context_safe is True

    with pytest.raises(TypeError):
        dtc.declare_safe("not a node")
    with pytest.raises(TypeError):
        dtc.declare_safe(dict)  # a class, but not a Node subclass


def test_declared_safe_inherited_by_subclass():
    from dtc.compiler import _is_declared_safe

    import support

    class Inherits(support.ContextPeekNode):
        pass

    class OptsOut(support.ContextPeekNode):
        dtc_context_safe = False

    assert _is_declared_safe(Inherits("name"))
    assert not _is_declared_safe(OptsOut("name"))


# --- dtc_context_writes declarations -----------------------------------------


def test_declared_writes_differential():
    assert_identical_and_compiled("{% capture r %}x={{ name }}{% endcapture %}{{ r }}")
    assert_identical_and_compiled(  # the motivating example: forloop in the body
        "{% capture csv %}{% for v in items %}{{ v }}"
        "{% if not forloop.last %},{% endif %}{% endfor %}{% endcapture %}[{{ csv }}]"
    )
    # capture inside a loop writes the loop scope: the value is visible after
    # the capture, gone after the loop pops — stock semantics, preserved.
    assert_identical_and_compiled(
        "{% for x in items %}{% capture s %}{{ x }}!{% endcapture %}{{ s }};"
        "{% endfor %}[{{ s }}]"
    )
    # read before and after the write
    assert_identical_and_compiled(
        "[{{ r }}]{% capture r %}v{% endcapture %}[{{ r }}]" + FLAT_MANY
    )


def test_declared_writes_keeps_flat_snapshot():
    template = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% capture stored %}v{% endcapture %}{{ stored }}" + FLAT_MANY
    )
    source = template._compiled.__dtc_source__
    assert "_flat_get('name'" in source  # snapshot survives the declared write
    assert "_flat_get('stored'" not in source  # the declared name walks


def test_declared_writes_scope_local_resync():
    """A declared write that shadows a loop-bound name must not leave the
    scope local stale: the bridge resyncs it."""
    source = (
        "{% for x in items %}{% capture x %}S{{ forloop.counter }}{% endcapture %}"
        "{{ x }};{% endfor %}"
    )
    assert_identical_and_compiled(source)
    template = make_backend(DTCTemplates, builtins=["support"]).from_string(source)
    compiled = template._compiled.__dtc_source__
    assert "_lv0_x" in compiled  # locals stay on
    assert "_lv0_x = _context_get('x')" in compiled  # ...resynced after the bridge
    assert template.render(base_context()) == "S1;S2;S3;"


def test_declared_writes_shareable():
    template = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% capture r %}v{% endcapture %}{{ r }}"
    )
    assert template._compiled.__dtc_shareable__


def test_check_declarations_allows_declared_writes(monkeypatch):
    monkeypatch.setenv("DTC_CHECK_DECLARATIONS", "1")
    assert_identical_and_compiled(
        "{% capture r %}x={{ name }}{% endcapture %}{{ r }}"
    )
    template = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% capture r %}v{% endcapture %}{{ r }}"
    )
    assert "_checked_safe_render(_node_0, context, _writes_0)" in (
        template._compiled.__dtc_source__
    )


def test_check_declarations_catches_undeclared_write(monkeypatch):
    """A writer declaring the wrong attribute (so no keys resolve) is a
    lying declaration: its real write must raise."""
    import dtc
    import support

    monkeypatch.setattr(
        support.ContextPokeNode, "dtc_context_writes", ("missing_attr",), raising=False
    )
    monkeypatch.setenv("DTC_CHECK_DECLARATIONS", "1")
    template = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% poke z %}"
    )
    with pytest.raises(dtc.ContextSafeViolation, match="ContextPokeNode"):
        template.render(base_context())


def test_declared_writes_via_attribute_name(monkeypatch):
    """ContextPokeNode honestly declared: var_name holds the written key,
    so the declaration unlocks flattening and passes check mode."""
    import support

    monkeypatch.setattr(
        support.ContextPokeNode, "dtc_context_writes", ("var_name",), raising=False
    )
    monkeypatch.setenv("DTC_CHECK_DECLARATIONS", "1")
    template = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% poke z %}{{ z }}" + FLAT_MANY
    )
    source = template._compiled.__dtc_source__
    assert "_flat_get('name'" in source
    assert "_flat_get('z'" not in source
    assert template.render(base_context()).startswith("<poke>poked")


def test_declare_writes_helper():
    import dtc
    from django import template

    class LocalSetter(template.Node):
        def __init__(self):
            self.target = "t"

        def render(self, context):
            context[self.target] = "v"
            return ""

    assert dtc.declare_writes(LocalSetter, "target") is LocalSetter
    assert LocalSetter.dtc_context_writes == ("target",)

    class Sub(LocalSetter):
        pass

    from dtc.compiler import _declared_writes

    assert _declared_writes(Sub()) == frozenset({"t"})  # inherited
    assert _declared_writes(LocalSetter()) == frozenset({"t"})

    with pytest.raises(TypeError):
        dtc.declare_writes(lambda ctx: "")  # not a Node class
    with pytest.raises(TypeError):
        dtc.declare_writes(LocalSetter, 42)  # attr names must be strings


def test_declared_writes_none_target_is_optional():
    """An attribute holding None (optional 'as var' unused) contributes no
    write; a non-string value voids the declaration conservatively."""
    from django import template

    from dtc.compiler import _declared_writes

    class OptionalSetter(template.Node):
        dtc_context_writes = ("dest",)

        def __init__(self, dest):
            self.dest = dest

        def render(self, context):
            if self.dest is not None:
                context[self.dest] = "v"
            return ""

    assert _declared_writes(OptionalSetter("x")) == frozenset({"x"})
    assert _declared_writes(OptionalSetter(None)) == frozenset()
    assert _declared_writes(OptionalSetter(42)) is None  # unusable: stays opaque


# --- root-layer writes ({% export %}) and the snapshot root exclusion --------


def test_export_differential():
    assert_identical_and_compiled("{% export answer 42 %}[{{ answer }}]")
    assert_identical_and_compiled(
        "{% export saved name %}{{ saved }}" + FLAT_MANY
    )
    assert_identical_and_compiled("{% export m missing %}[{{ m }}]")
    # A root write shadowed by a loop scope: reads inside the loop see the
    # loop variable, reads after see the exported value.
    assert_identical_and_compiled(
        "{% for x in items %}{% export x 'deep' %}{{ x }};{% endfor %}[{{ x }}]"
    )


def test_export_persists_across_include():
    """The tag's purpose: exported from an included template, read in the
    parent — through the parent's flat snapshot."""
    templates = {
        "main.html": FLAT_MANY + "{% include 'sets.html' %}[{{ answer }}]",
        "sets.html": "{% export answer 42 %}",
    }
    template, out = render_named_both(templates, "main.html")
    assert out.endswith("[42]")
    assert "_flat_get" in template._compiled.__dtc_source__  # snapshot active


def test_export_rewrite_not_served_stale():
    """The case the root exclusion exists for: a unit whose snapshot was
    taken while an exported name was already set, with a nested template
    re-exporting it before a later read. A snapshot including the root
    layer would serve the first value; stock serves the second."""
    templates = {
        "main.html": "{% include 'first.html' %}{% include 'reader.html' %}",
        "first.html": "{% export rx 'v1' %}",
        "reader.html": FLAT_MANY + "[{{ rx }}]{% include 'second.html' %}[{{ rx }}]",
        "second.html": "{% export rx 'v2' %}",
    }
    template, out = render_named_both(templates, "main.html")
    assert out.endswith("[v1][v2]")


def test_check_declarations_allows_root_write(monkeypatch):
    monkeypatch.setenv("DTC_CHECK_DECLARATIONS", "1")
    assert_identical_and_compiled("{% export answer 42 %}[{{ answer }}]")


def test_export_shareable():
    template = make_backend(DTCTemplates, builtins=["support"]).from_string(
        "{% export n 1 %}"
    )
    assert template._compiled.__dtc_shareable__


def test_known_limitation_intermediate_layer_writes():
    """Documents the one compatibility boundary (README "Limitation"): a
    tag that mutates an INTERMEDIATE context layer from inside an include
    can stale the enclosing template's snapshot or scope locals — the
    enclosing template compiles without knowledge of the tag. This test
    asserts the divergence deliberately: if it starts failing, the
    boundary moved and the README/CLAUDE.md must be updated with it.
    (Same-template use of such tags stays exact and differential-tested;
    root-layer writes are exact via the snapshot's dicts[0] exclusion.)"""
    from django import template as dj_template

    lib = dj_template.Library()

    class LayerSurgeryNode(dj_template.Node):
        def render(self, context):
            context.set_upward("marker", "changed")  # nearest enclosing layer
            return ""

    @lib.tag
    def layer_surgery(parser, token):
        return LayerSurgeryNode()

    templates = {
        "parent.html": FLAT_MANY + "{% include 'surgeon.html' %}[{{ marker }}]",
        "surgeon.html": "{% layer_surgery %}",
    }
    options = {
        "loaders": [("django.template.loaders.locmem.Loader", templates)],
        "builtins": ["support"],
    }
    context = dict(base_context(), marker="original")

    dtc_backend = make_backend(DTCTemplates, **options)
    django_backend = make_backend(DjangoTemplates, **options)
    # Register the library on both engines directly (it's test-local).
    for backend in (dtc_backend, django_backend):
        backend.engine.template_builtins.append(lib)

    stock = django_backend.get_template("parent.html").render(dict(context))
    dtc_template = dtc_backend.get_template("parent.html")
    assert dtc_template._compiled is not None
    out = dtc_template.render(dict(context))

    assert stock.endswith("[changed]")  # stock sees the mutation immediately
    assert out.endswith("[original]")  # dtc's snapshot predates it: the
    # documented divergence — exactness holds only for scope-limited or
    # root-layer cross-template effects
