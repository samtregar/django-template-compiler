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


def test_forloop_forced_by_block_in_loop():
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
