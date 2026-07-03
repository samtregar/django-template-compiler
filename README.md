# django-template-compiler

A drop-in replacement for Django's template engine, 100% compatible including custom tags and filters, but much faster.

**Status: pre-alpha, but substantially complete.** Every parseable template compiles: dedicated code generation for the core template language (variables, filters, control flow, inheritance, `simple_tag`/`inclusion_tag`, container tags), with anything else — arbitrary third-party tags included — running as-is against the live context. dtc passes Django's own template test suite (Django 4.2–5.2) in CI, plus a differential fuzzer. Typical speedups: 1.6–2.1x on template-bound rendering, with a ~1.0x floor when a template is dominated by bridged tags. Not yet exercised by production traffic — try it and report.

Two behaviors worth knowing:

- **`DEBUG=True` disables compilation** (per engine): Django's debug error page and exception annotation need the interpreted render path. Production configs get the compiled path; development keeps perfect debugging.
- **Django test instrumentation is honored**: when `setup_test_environment()` (the test runner / `assertTemplateUsed`) patches template rendering, dtc detects the patch and routes through it, so the `template_rendered` signal fires exactly as with stock Django.

## How it works

Templates are parsed with Django's own lexer and parser, then compiled to Python code — a `{% for %}` loop becomes a real Python `for` loop, variable lookups become direct attribute/key access. Anything the compiler can't handle yet (including arbitrary custom tags) falls back to Django's interpreted render path, so output is always exactly what Django would produce.

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

## Development

```bash
pip install -e .[dev]
pytest
```

## License

BSD 3-Clause. See [LICENSE](LICENSE).
