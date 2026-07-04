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
    yield (
        "loop_simple (100 rows, no forloop)",
        "{% for row in rows %}<li>{{ row }}</li>{% endfor %}",
        {"rows": [f"row {i}" for i in range(100)]},
    )
    yield (
        "loop_forloop (100 rows, counters)",
        "{% for row in rows %}<li>{{ forloop.counter }}: {{ row }}</li>{% endfor %}",
        {"rows": [f"row {i}" for i in range(100)]},
    )
    yield (
        "table (50x4, if + nested loop)",
        "{% for row in table %}<tr {% if forloop.first %}class='f'{% endif %}>"
        "{% for cell in row %}<td>{{ cell }}</td>{% endfor %}</tr>{% endfor %}",
        {"table": [[f"c{r}.{c}" for c in range(4)] for r in range(50)]},
    )
    yield (
        "with_if (scopes and branches)",
        "{% for row in rows %}{% with v=row %}{% if v %}{{ v }}{% else %}-{% endif %}"
        "{% endwith %}{% endfor %}",
        {"rows": [f"row {i}" for i in range(50)]},
    )
    yield (
        "container (spaceless around table)",
        "{% spaceless %}{% for row in table %}<tr> {% for cell in row %}"
        "<td> {{ cell }} </td> {% endfor %}</tr>{% endfor %}{% endspaceless %}",
        {"table": [[f"c{r}.{c}" for c in range(4)] for r in range(25)]},
    )
    yield (
        "ifchanged (grouped rows)",
        "{% for p in people %}{% ifchanged p.0 %}<h2>{{ p.0 }}</h2>{% endifchanged %}"
        "<p>{{ p.1 }}</p>{% endfor %}",
        {"people": [(f"team{i // 10}", f"member{i}") for i in range(50)]},
    )
    # A tag without dedicated codegen bridges per-node; the surrounding
    # variables still compile. The floor is "no slower than stock".
    yield (
        "tag_bridged (worst case)",
        "{% now 'Y' %} " + " ".join("{{ v%d }}" % i for i in range(10)),
        {f"v{i}": f"value {i}" for i in range(10)},
    )


INHERITANCE_TEMPLATES = {
    "bench_base.html": (
        "<html><head><title>{% block title %}t{% endblock %}</title></head>"
        "<body><nav>{% block nav %}{% for s in sections %}<a>{{ s }}</a>"
        "{% endfor %}{% endblock %}</nav>"
        "<main>{% block content %}{% endblock %}</main></body></html>"
    ),
    "bench_child.html": (
        "{% extends 'bench_base.html' %}"
        "{% block title %}{{ title }} | {{ block.super }}{% endblock %}"
        "{% block content %}{% for row in rows %}"
        "<p>{% include 'bench_item.html' %}</p>{% endfor %}{% endblock %}"
    ),
    "bench_item.html": "<b>{{ row }}</b> in {{ title }}",
}


def inheritance_scenario():
    return (
        "inheritance (extends + include in loop)",
        "bench_child.html",
        {
            "title": "Page",
            "sections": ["a", "b", "c"],
            "rows": [f"row {i}" for i in range(20)],
        },
    )


def bench(template, context):
    timer = timeit.Timer(lambda: template.render(dict(context)))
    number, _ = timer.autorange()
    return min(timer.repeat(repeat=5, number=number)) / number


def make_backend(cls):
    return cls(
        {
            "NAME": "bench",
            "DIRS": [],
            "APP_DIRS": False,
            "OPTIONS": {
                # cached.Loader matches production (Django wraps DIRS/APP_DIRS
                # loaders with it automatically when loaders aren't given).
                "loaders": [
                    (
                        "django.template.loaders.cached.Loader",
                        [
                            (
                                "django.template.loaders.locmem.Loader",
                                INHERITANCE_TEMPLATES,
                            )
                        ],
                    )
                ]
            },
        }
    )


def main():
    dtc_backend = make_backend(DTCTemplates)
    django_backend = make_backend(DjangoTemplates)

    print(f"{'scenario':38} {'django':>10} {'dtc':>10} {'speedup':>9}")
    for name, source, context in scenarios():
        dtc_template = dtc_backend.from_string(source)
        django_template = django_backend.from_string(source)
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

    try:
        import jinja2
    except ImportError:
        jinja2 = None
    if jinja2 is not None:
        # Reference point, not a target: Jinja2 changes the template
        # language to get its speed; dtc's constraint is byte-identical
        # Django output. Same table scenario, hand-translated.
        env = jinja2.Environment(autoescape=True)
        jinja_template = env.from_string(
            "{% for row in table %}<tr {% if loop.first %}class='f'{% endif %}>"
            "{% for cell in row %}<td>{{ cell }}</td>{% endfor %}</tr>{% endfor %}"
        )
        table = {"table": [[f"c{r}.{c}" for c in range(4)] for r in range(50)]}
        timer = timeit.Timer(lambda: jinja_template.render(**table))
        number, _ = timer.autorange()
        jinja_time = min(timer.repeat(repeat=5, number=number)) / number
        print(f"{'  (jinja2 reference, table scenario)':38} {'':>10} {jinja_time * 1e6:8.1f}us")

    name, template_name, context = inheritance_scenario()
    dtc_template = dtc_backend.get_template(template_name)
    django_template = django_backend.get_template(template_name)
    assert dtc_template._compiled is not None, f"{name} did not compile"
    assert dtc_template.render(dict(context)) == django_template.render(dict(context))
    django_time = bench(django_template, context)
    dtc_time = bench(dtc_template, context)
    print(
        f"{name:38} {django_time * 1e6:8.1f}us {dtc_time * 1e6:8.1f}us"
        f" {django_time / dtc_time:8.2f}x"
    )


def cold_start_report(count=100):
    """Per-template cost of getting to a renderable state on process start:
    Django's parse alone, dtc parse+compile, and dtc with the disk cache."""
    import shutil
    import tempfile
    import time

    source_for = lambda i: (
        f"<!-- {i} -->{{% for row in table %}}<tr {{% if forloop.first %}}c{{% endif %}}>"
        "{% for cell in row %}<td>{{ cell|upper }}</td>{% endfor %}</tr>{% endfor %}"
        "{{ a }}{{ b }}{{ c }}{{ d }}{{ e }}{{ f }}{{ g }}"
    )

    def measure(make, label):
        start = time.perf_counter()
        for i in range(count):
            make(source_for(i))
        per = (time.perf_counter() - start) / count
        print(f"{label:38} {per * 1e6:8.1f}us/template")

    django_backend = make_backend(DjangoTemplates)
    measure(django_backend.from_string, "cold start: django parse only")
    measure(make_backend(DTCTemplates).from_string, "cold start: dtc parse+compile")

    cache_dir = tempfile.mkdtemp(prefix="dtc-bench-cache-")
    try:
        opts = {
            "NAME": "bench",
            "DIRS": [],
            "APP_DIRS": False,
            "OPTIONS": {"dtc_disk_cache": cache_dir},
        }
        measure(DTCTemplates(dict(opts)).from_string, "cold start: dtc cache cold (writes)")
        measure(DTCTemplates(dict(opts)).from_string, "cold start: dtc cache warm (hits)")
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
    print()
    cold_start_report()
