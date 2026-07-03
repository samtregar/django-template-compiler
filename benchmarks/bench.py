"""Benchmark dtc against Django's stock template engine.

Usage: .venv/bin/python benchmarks/bench.py

Compares per-render time on scenarios chosen to bracket phase 1: variable-
heavy (best case for the compiler), dotted lookups, text-heavy, and a
filter-heavy worst case (filters are bridged to the original nodes in
phase 1, so this should be ~1x, not slower).
"""

import timeit

import django
from django.conf import settings

settings.configure(DEBUG=False, USE_TZ=True)
django.setup()

from django.template.backends.django import DjangoTemplates  # noqa: E402

from dtc.backend import DTCTemplates  # noqa: E402


class Obj:
    def __init__(self):
        self.name = "Widget"
        self.price = "9.99"
        self.category = "tools"


def scenarios():
    var_ctx = {f"v{i}": f"value {i}" for i in range(40)}
    yield (
        "var_heavy (40 plain vars)",
        " ".join("{{ v%d }}" % i for i in range(40)),
        var_ctx,
    )
    yield (
        "dotted (20 x 2-level lookups)",
        " ".join(["{{ obj.name }} {{ d.key }}"] * 10),
        {"obj": Obj(), "d": {"key": "value"}},
    )
    yield (
        "text_heavy (4 vars in prose)",
        ("Lorem ipsum dolor sit amet " * 40).join(
            ["", "{{ a }}", "{{ b }}", "{{ c }}", "{{ d }}"]
        ),
        {"a": "A", "b": "B", "c": "C", "d": "D"},
    )
    yield (
        "filters_light (20 x upper)",
        " ".join(["{{ a|upper }} {{ b|lower }}"] * 10),
        {"a": "hello", "b": "WORLD"},
    )
    yield (
        "filters (20 x chained/args)",
        " ".join(["{{ a|upper|truncatechars:8 }} {{ b|join:', ' }}"] * 10),
        {"a": "hello world", "b": ["x", "y", "z"]},
    )
    # An uncompilable tag forces whole-template fallback: the floor is
    # "no slower than stock", not a speedup.
    yield (
        "tag_fallback (worst case)",
        "{% now 'Y' %} " + " ".join("{{ v%d }}" % i for i in range(10)),
        {f"v{i}": f"value {i}" for i in range(10)},
    )


def bench(template, context):
    timer = timeit.Timer(lambda: template.render(dict(context)))
    number, _ = timer.autorange()
    return min(timer.repeat(repeat=5, number=number)) / number


def make_backend(cls):
    return cls({"NAME": "bench", "DIRS": [], "APP_DIRS": False, "OPTIONS": {}})


def main():
    dtc_backend = make_backend(DTCTemplates)
    django_backend = make_backend(DjangoTemplates)

    print(f"{'scenario':38} {'django':>10} {'dtc':>10} {'speedup':>9}")
    for name, source, context in scenarios():
        dtc_template = dtc_backend.from_string(source)
        django_template = django_backend.from_string(source)
        if "fallback" not in name:
            assert dtc_template._compiled is not None, f"{name} did not compile"
        assert dtc_template.render(dict(context)) == django_template.render(
            dict(context)
        ), f"{name} output mismatch"
        django_time = bench(django_template, context)
        dtc_time = bench(dtc_template, context)
        print(
            f"{name:38} {django_time * 1e6:8.1f}us {dtc_time * 1e6:8.1f}us"
            f" {django_time / dtc_time:8.2f}x"
        )


if __name__ == "__main__":
    main()
