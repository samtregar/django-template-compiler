# django-template-compiler

A drop-in replacement for Django's template engine, 100% compatible including custom tags and filters, but much faster.

**Status: pre-alpha.** The engine backend works today by delegating to Django's renderer; the compiler that makes it fast is under construction. Not ready for production use.

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
