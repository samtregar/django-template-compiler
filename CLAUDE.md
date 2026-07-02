# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project goal

A drop-in replacement for Django's template engine that compiles templates to Python for speed, with **100% compatibility** as a hard requirement — including third-party custom tags and filters. Output must be byte-identical to Django's stock engine in all cases. When compatibility and speed conflict, compatibility wins: anything the compiler can't handle correctly must fall back to Django's interpreted renderer rather than approximate it.

## Naming

The PyPI distribution name is `django-template-compiler`; the import name is `dtc` (the bare name `dtc` is taken on PyPI). Source lives in `src/dtc/`.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # one-time setup
.venv/bin/pytest                                             # run tests
.venv/bin/pytest tests/test_backend.py -k differential       # run a subset
.venv/bin/python -m build && .venv/bin/twine check dist/*    # build + validate dists
```

Version is single-sourced from `__version__` in `src/dtc/__init__.py` (hatchling dynamic version). CI (`.github/workflows/ci.yml`) tests Python 3.10–3.13 × Django 4.2/5.1/5.2 — code must work across that whole matrix.

## Architecture

The design is **hybrid: compile what we can, bridge what we can't.**

- Templates are parsed by Django's own `Lexer`/`Parser` (never reimplement parsing — reusing it is a deliberate compatibility decision). Only the render path is replaced.
- `src/dtc/backend.py` — `DTCTemplates`, a `BACKENDS` engine that subclasses Django's stock `DjangoTemplates` backend, inheriting all configuration/loader/OPTIONS handling unchanged. Its `Template` proxy calls `compile_template()` and uses the result if non-`None`, otherwise delegates to Django's `Template.render`.
- `src/dtc/compiler.py` — the compiler. Contract: `compile_template(template) -> callable(Context) -> str | None`. Returning `None` means "not compilable yet"; the fallback makes the engine correct by construction while codegen coverage grows tag-by-tag. Currently a stub that always returns `None`.

Known-hard problem baked into this design: compiled code that uses fast local variables must still expose live, mutable context state to bridged third-party `Node.render(context)` calls (context escape hatch). Keep this in mind when designing codegen.

## Testing policy

The compatibility oracle is differential testing: `tests/test_backend.py` renders each case through both dtc and Django's stock backend and asserts identical output. Any new compiler capability needs differential cases covering it, including edge semantics (autoescape/`mark_safe`, silent `VariableDoesNotExist`, context push/pop scoping, `forloop`/`parentloop`, `{{ block.super }}`). The long-term goal is to run Django's own `template_tests` suite against this engine.
