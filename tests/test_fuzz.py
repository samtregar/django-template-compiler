"""Differential fuzzer: the second oracle layer.

Generates random templates from a grammar covering the compiled feature set,
with random contexts (HTML-unsafe strings, SafeStrings, numbers, None,
nested containers, objects with attributes/methods/properties, callables,
absent names), renders each through dtc and stock Django, and requires
byte-identical output — or an identical exception type.

Each run uses a fresh random seed (printed on failure); reproduce with
DTC_FUZZ_SEED=<seed>. DTC_FUZZ_ITERS overrides the iteration count.
"""

import os
import random
import time

import pytest
from django.template.backends.django import DjangoTemplates
from django.utils.safestring import mark_safe

from dtc.backend import DTCTemplates

from support import make_backend

ITERATIONS = int(os.environ.get("DTC_FUZZ_ITERS", "300"))
SEED = int(os.environ.get("DTC_FUZZ_SEED", "0")) or int(time.time() * 1000) % 10**9

NAMES = ["a", "b", "c", "d", "e", "obj", "seq", "map", "missing"]
LOOKUP_BITS = ["attr", "key", "0", "1", "method", "prop", "nope"]
FILTERS = [
    "upper",
    "lower",
    "title",
    "capfirst",
    "length",
    "default:'D<'",
    "join:', '",
    "add:2",
    "add:b",
    "truncatechars:5",
    "floatformat:1",
    "safe",
    "escape",
    "striptags",
    "slice:':2'",
]
OPERATORS = ["==", "!=", "<", ">", "<=", ">=", "in", "not in"]
LITERALS = ["'x<y'", "2", "2.5", "True", "None", "'0'"]


class Thing:
    def __init__(self, attr):
        self.attr = attr

    def method(self):
        return "called<>"

    @property
    def prop(self):
        return 3.5


def make_value(rng, depth=0):
    kinds = ["html", "plain", "int", "float", "none", "bool", "safe", "callable"]
    if depth < 2:
        kinds += ["list", "dict", "obj", "list", "dict"]
    kind = rng.choice(kinds)
    if kind == "html":
        return rng.choice(['<b>&"x</b>', "a<c", "&amp;", "'quoted'"])
    if kind == "plain":
        return rng.choice(["hello", "Wörld", "", "one two three"])
    if kind == "int":
        return rng.choice([0, 1, -3, 42, 10**9])
    if kind == "float":
        return rng.choice([0.0, 2.5, -1.75, 1234.5678])
    if kind == "none":
        return None
    if kind == "bool":
        return rng.choice([True, False])
    if kind == "safe":
        return mark_safe("<i>safe&</i>")
    if kind == "callable":
        return lambda: "fn<>&"
    if kind == "list":
        return [make_value(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    if kind == "dict":
        return {
            key: make_value(rng, depth + 1)
            for key in rng.sample(["key", "0", "x", "attr"], k=rng.randint(1, 3))
        }
    return Thing(make_value(rng, depth + 1))


def make_context(rng):
    present = [name for name in NAMES if name != "missing" and rng.random() > 0.15]
    return {name: make_value(rng) for name in present}


def gen_var(rng):
    bits = [rng.choice(NAMES)]
    for _ in range(rng.randint(0, 2)):
        bits.append(rng.choice(LOOKUP_BITS))
    filters = "".join(f"|{rng.choice(FILTERS)}" for _ in range(rng.randint(0, 2)))
    return "{{ %s%s }}" % (".".join(bits), filters)


def gen_condition(rng, depth=0):
    if depth < 2 and rng.random() < 0.4:
        joiner = rng.choice([" and ", " or "])
        return gen_condition(rng, depth + 1) + joiner + gen_condition(rng, depth + 1)
    operand = rng.choice(NAMES)
    roll = rng.random()
    if roll < 0.3:
        return operand
    if roll < 0.4:
        return f"not {operand}"
    return f"{operand} {rng.choice(OPERATORS)} {rng.choice(LITERALS + NAMES)}"


def gen_nodes(rng, depth=0):
    parts = []
    for _ in range(rng.randint(1, 4)):
        roll = rng.random()
        if roll < 0.25:
            parts.append(rng.choice(["text ", "<p>markup</p>", " & raw ", "x"]))
        elif roll < 0.55 or depth >= 3:
            parts.append(gen_var(rng))
        elif roll < 0.67:
            body = gen_nodes(rng, depth + 1)
            tail = ""
            if rng.random() < 0.4:
                tail = "{% elif " + gen_condition(rng) + " %}" + gen_nodes(rng, depth + 1)
            if rng.random() < 0.5:
                tail += "{% else %}" + gen_nodes(rng, depth + 1)
            parts.append(
                "{% if " + gen_condition(rng) + " %}" + body + tail + "{% endif %}"
            )
        elif roll < 0.82:
            loopvars = "k, v" if rng.random() < 0.2 else "item"
            seq = rng.choice(NAMES)
            reversed_ = " reversed" if rng.random() < 0.2 else ""
            body = gen_nodes(rng, depth + 1)
            if rng.random() < 0.3:
                body += "{{ forloop.counter }}"
            if rng.random() < 0.15:
                body += "{{ forloop.parentloop.counter0 }}"
            empty = (
                "{% empty %}" + gen_nodes(rng, depth + 1)
                if rng.random() < 0.3
                else ""
            )
            parts.append(
                f"{{% for {loopvars} in {seq}{reversed_} %}}{body}{empty}{{% endfor %}}"
            )
        elif roll < 0.88:
            parts.append(
                "{% with w="
                + rng.choice(NAMES)
                + " %}"
                + gen_nodes(rng, depth + 1)
                + "{{ w }}{% endwith %}"
            )
        elif roll < 0.92:
            setting = rng.choice(["on", "off"])
            parts.append(
                f"{{% autoescape {setting} %}}"
                + gen_nodes(rng, depth + 1)
                + "{% endautoescape %}"
            )
        elif roll < 0.96:
            parts.append(
                "{% spaceless %}<p> " + gen_nodes(rng, depth + 1) + " </p>{% endspaceless %}"
            )
        else:
            parts.append(
                "{% filter upper %}" + gen_nodes(rng, depth + 1) + "{% endfilter %}"
            )
    return "".join(parts)


def render(backend_cls, source, context):
    template = make_backend(backend_cls).from_string(source)
    try:
        return "ok", template.render(dict(context))
    except Exception as exc:  # compared by type: reprs may embed object ids
        return "raised", type(exc).__name__


def test_fuzz_differential():
    rng = random.Random(SEED)
    for i in range(ITERATIONS):
        source = gen_nodes(rng)
        context = make_context(rng)
        dtc_result = render(DTCTemplates, source, context)
        django_result = render(DjangoTemplates, source, context)
        assert dtc_result == django_result, (
            f"divergence at iteration {i} (reproduce: DTC_FUZZ_SEED={SEED})\n"
            f"template: {source!r}\ncontext: {context!r}\n"
            f"dtc:    {dtc_result!r}\ndjango: {django_result!r}"
        )