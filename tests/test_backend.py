"""Tests for the dtc engine backend.

The differential tests are the important pattern here: every template is
rendered through both dtc and Django's stock backend, and the outputs must
match exactly. As the compiler grows, these tests keep it honest.
"""

from pathlib import Path

import pytest
from django.template import TemplateDoesNotExist
from django.template.backends.django import DjangoTemplates

from dtc.backend import DTCTemplates

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


@pytest.fixture
def dtc_backend():
    return make_backend(DTCTemplates)


@pytest.fixture
def django_backend():
    return make_backend(DjangoTemplates)


def test_from_string(dtc_backend):
    template = dtc_backend.from_string("Hello {{ name }}!")
    assert template.render({"name": "world"}) == "Hello world!"


def test_autoescape(dtc_backend):
    template = dtc_backend.from_string("{{ value }}")
    assert template.render({"value": "<b>"}) == "&lt;b&gt;"


def test_autoescape_off(dtc_backend):
    template = dtc_backend.from_string(
        "{% autoescape off %}{{ value }}{% endautoescape %}"
    )
    assert template.render({"value": "<b>"}) == "<b>"


def test_missing_variable_renders_empty(dtc_backend):
    template = dtc_backend.from_string("[{{ missing }}]")
    assert template.render({}) == "[]"


def test_get_template_missing(dtc_backend):
    with pytest.raises(TemplateDoesNotExist):
        dtc_backend.get_template("no-such-template.html")


DIFFERENTIAL_CASES = [
    ("Hello {{ name }}!", {"name": "world"}),
    ("{{ value|upper }} {{ value|length }}", {"value": "abc"}),
    ("{% if x %}yes{% else %}no{% endif %}", {"x": True}),
    ("{% if x %}yes{% else %}no{% endif %}", {"x": False}),
    (
        "{% for a in items %}{{ forloop.counter0 }}:{{ a }} {% endfor %}",
        {"items": ["x", "y", "z"]},
    ),
    ("{% with total=1 %}{{ total }}{% endwith %}{{ total }}", {}),
    ("{{ user.name|default:'anonymous' }}", {"user": {}}),
    ("{{ html }}", {"html": "<script>"}),
]


@pytest.mark.parametrize("source,context", DIFFERENTIAL_CASES)
def test_differential_from_string(dtc_backend, django_backend, source, context):
    """dtc output must match Django's stock engine exactly."""
    expected = django_backend.from_string(source).render(dict(context))
    actual = dtc_backend.from_string(source).render(dict(context))
    assert actual == expected


@pytest.mark.parametrize("name", ["base.html", "child.html", "snippet.html"])
def test_differential_files(dtc_backend, django_backend, name):
    context = {"title": "Page", "items": ["one", "two"]}
    expected = django_backend.get_template(name).render(dict(context))
    actual = dtc_backend.get_template(name).render(dict(context))
    assert actual == expected


def test_version():
    import dtc

    assert dtc.__version__
