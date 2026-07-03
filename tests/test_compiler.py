"""Differential tests for the phase 1 compiler (text + variables).

Every case renders through both dtc and Django's stock backend and must
produce identical output. Cases marked compiled also assert that dtc
actually took the compiled path — otherwise these tests would silently
compare Django with Django.
"""

import datetime

import pytest
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
    }


# --- machinery ---------------------------------------------------------------


def render_both(source, context=None, **options):
    """Render through dtc and stock Django; return (dtc_template, dtc_out, django_out)."""
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
    # filters (bridged through the original node in phase 1)
    "{{ name|upper }} {{ html|title }}",
    "{{ missing|default:'fallback' }}",
    "{{ html|safe }}",
    # everything at once
    "<p>{{ name }} bought {{ obj.name }} for {{ f }} at {{ d.key }}</p>",
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


# --- compiled/fallback classification ----------------------------------------


def test_tags_fall_back():
    template = make_backend(DTCTemplates).from_string("{% now 'Y' %}")
    assert template._compiled is None


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
    assert "_node_2.render(context)" in source  # slow path bridges to the node
