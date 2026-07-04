# django-template-compiler

A drop-in replacement for Django's template engine, 100% compatible including custom tags and filters, but much faster.

**Status: pre-alpha, but substantially complete.** Every parseable template compiles: dedicated code generation for the core template language (variables, filters, control flow, inheritance, `simple_tag`/`inclusion_tag`, container tags), with anything else — arbitrary third-party tags included — running as-is against the live context. dtc passes Django's own template test suite (Django 4.2–5.2) in CI, plus a differential fuzzer. Typical speedups: 1.6–2.1x on template-bound rendering, with a ~1.0x floor when a template is dominated by bridged tags. Not yet exercised by production traffic — try it and report.

Two behaviors worth knowing:

- **`DEBUG=True` disables compilation** (per engine): Django's debug error page and exception annotation need the interpreted render path. Production configs get the compiled path; development keeps perfect debugging.
- **Django test instrumentation is honored**: when `setup_test_environment()` (the test runner / `assertTemplateUsed`) patches template rendering, dtc detects the patch and routes through it, so the `template_rendered` signal fires exactly as with stock Django.

## How it works

Templates are parsed with Django's own lexer and parser, then compiled to Python code — a `{% for %}` loop becomes a real Python `for` loop, variable lookups become direct attribute/key access. Anything the compiler can't handle yet (including arbitrary custom tags) falls back to Django's interpreted render path, so output is always exactly what Django would produce.

## Benchmarks

`benchmarks/bench.py`, Python 3.11, Django 5.2 (µs per render; higher speedup is better):

| scenario | django | dtc | speedup |
|---|---:|---:|---:|
| 40 plain variables | 52.2 | 28.6 | 1.8x |
| 100-row loop | 165.7 | 73.2 | 2.3x |
| 100-row loop with `forloop.counter` | 679.4 | 126.9 | **5.4x** |
| 50×4 table (nested loop + if) | 516.3 | 271.9 | 1.9x |
| with/if scopes | 206.8 | 91.2 | 2.3x |
| spaceless-wrapped table | 248.2 | 114.6 | 2.2x |
| inheritance + include in loop | 153.6 | 135.0 | 1.1x |
| bridged unknown tag (worst case) | 26.3 | 21.0 | 1.3x |

For reference, Jinja2 renders the table scenario in ~80µs — dtc closes about half the gap to Jinja2 while producing byte-identical Django output. The remaining distance is the price of Django's semantics themselves (silent variable failures, callable auto-invocation, the context stack), which dtc preserves exactly and Jinja2 deliberately dropped.

## Installation

```bash
pip install django-template-compiler
```

The import name is `dtc`.

## Usage

Change one line in your `TEMPLATES` setting:

```python
TEMPLATES = [
    {
        "BACKEND": "dtc.backend.DTCTemplates",  # was django.template.backends.django.DjangoTemplates
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            # all DjangoTemplates options work unchanged
            "context_processors": [...],
        },
    },
]
```

Everything else — template syntax, custom tag libraries, context processors, `{% load %}`, filters — works unchanged.

### Cold starts and the disk cache

Compiling costs roughly 9x Django's parse per template, paid once per process. If your deployment restarts processes often (serverless, aggressive autoscaling), enable the disk cache, which persists compiled code objects across processes and cuts that overhead by ~70%:

```python
"OPTIONS": {
    "dtc_disk_cache": True,  # ~/.cache/dtc/..., or pass an explicit path
},
```

Cache entries are keyed by a hash of the generated code, so stale entries are impossible by construction; corrupt or version-mismatched entries are silently recompiled. Point it only at a directory you trust — cached code is executed.

## Development

```bash
pip install -e .[dev]
pytest
```

## License

BSD 3-Clause. See [LICENSE](LICENSE).
