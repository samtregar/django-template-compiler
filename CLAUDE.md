# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project goal

A drop-in replacement for Django's template engine that compiles templates to Python for speed, with **100% compatibility** as a hard requirement — including third-party custom tags and filters. Output must be byte-identical to Django's stock engine in all cases. When compatibility and speed conflict, compatibility wins: anything the compiler can't handle correctly must fall back to Django's interpreted renderer rather than approximate it.

Work proceeds in phases — see `ROADMAP.md` for the current phase and what's deliberately deferred. Phases 1–7 are complete; phase 8 (performance) remains.

Correctness invariants to preserve when changing the compiler:
- Oracle suites run **strict** (`DTC_STRICT`): compiler errors fail CI rather than falling back. Keep it that way.
- Compiled shortcuts must honor a patched `Template._render` (test instrumentation, autopatch, third-party hooks) — see `runtime._render_is_patched`.
- Templates whose compiled function embeds bridged or identity-keyed-state nodes are non-shareable across parses (`__dtc_shareable__`).
- `tests/test_fuzz.py` fuzzes with a fresh seed every run; a CI fuzz failure is a real bug — reproduce with the printed `DTC_FUZZ_SEED`.

## Naming

The PyPI distribution name is `django-template-compiler`; the import name is `dtc` (the bare name `dtc` is taken on PyPI). Source lives in `src/dtc/`.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # one-time setup
.venv/bin/pytest                                             # run tests
.venv/bin/pytest tests/test_backend.py -k differential       # run a subset
.venv/bin/python -m build && .venv/bin/twine check dist/*    # build + validate dists
.venv/bin/python benchmarks/bench.py                         # speed vs stock engine
.venv/bin/python scripts/run_django_suite.py <django-src>    # Django's own template_tests vs dtc
```

For the last command, `<django-src>` is a Django source checkout whose tag matches the installed version (the script verifies): `git clone --depth 1 --branch $(python -c 'import django; print(django.__version__)') https://github.com/django/django.git`. It works by installing `dtc.autopatch`, which patches `Template._render` engine-wide (and `django.test.utils.instrumented_test_render`, preserving the `template_rendered` signal) — that's what routes Django's engine-level test templates through the compiler. It prints compiled/fallback stats at exit; a run where `renders_compiled` is 0 proved nothing.

Version is single-sourced from `__version__` in `src/dtc/__init__.py` (hatchling dynamic version). CI (`.github/workflows/ci.yml`) tests Python 3.10–3.13 × Django 4.2/5.1/5.2 — code must work across that whole matrix.

## Architecture

The design is **hybrid: compile what we can, bridge what we can't.**

- Templates are parsed by Django's own `Lexer`/`Parser` (never reimplement parsing — reusing it is a deliberate compatibility decision). Only the render path is replaced.
- `src/dtc/backend.py` — `DTCTemplates`, a `BACKENDS` engine that subclasses Django's stock `DjangoTemplates` backend, inheriting all configuration/loader/OPTIONS handling unchanged. Its `Template` proxy calls `compile_template()` and uses the result if non-`None`, otherwise delegates to Django's `Template.render`. The compiled callable replaces `Template._render` only; the proxy reproduces `Template.render`'s observable bookkeeping (`render_context.push_state`, `bind_template` — which runs context processors and exposes `context.template.engine`) around it.
- `src/dtc/compiler.py` — the compiler. Contract: `compile_template(template) -> callable(Context) -> str | None`. Returning `None` means "not compilable yet"; the fallback makes the engine correct by construction while codegen coverage grows tag-by-tag. Phases 1–6 are implemented: **every parseable template compiles** (except under debug engines). Dedicated codegen covers text, variables (filters call the registered functions directly, with `is_safe`/`needs_autoescape`/`expects_localtime` specialization decided at compile time — custom filters compile natively), `{% if %}`, `{% for %}`, `{% with %}`, `{% autoescape %}`, `{% comment %}`, `{% verbatim %}`, `{% load %}`, inheritance (`{% block %}`/`{% extends %}`/`{% include %}`), `@simple_tag`, `@inclusion_tag`, and the container tags (`{% spaceless %}`, `{% filter %}`, `{% ifchanged %}`, `{% localize %}`, `{% localtime %}`, `{% timezone %}`, `{% language %}` — containers matter because bridging one forces its subtree to render interpreted); any other node bridges as `render_annotated(context)`, exact because compiled code performs *real* `context.push()/pop()/__setitem__` operations. Leaf tags (`url`, `csrf_token`, `static`, ...) stay bridged deliberately: their render is the work itself. The `forloop` dict is elided when the analysis pass proves it unreferenced; bridged nodes, blocks, includes, and `takes_context` tags inside a loop disable elision (unseen content may reference `forloop`; `IfChangedNode` even uses the dict as its state frame). Compiled functions embedding bridged nodes are marked non-shareable (`__dtc_shareable__`) — stateful nodes key state by node identity, so per-parse functions must stay per-parse.
- `src/dtc/runtime.py` — render-time support: `compiled_for()` (per-instance + per-(engine, name, source) compile caching — the latter prevents recompiling per render under non-cached loaders) and mirrors of `BlockNode`/`ExtendsNode`/`IncludeNode.render` with the template-render call made compiled-aware. Block bodies compile to standalone functions attached to BlockNodes as `_dtc_body`, linked at render time through Django's own `BlockContext`, so mixed compiled/interpreted inheritance chains work in both directions. The mirrors are verified byte-identical across Django 4.2–5.2; the CI oracle suites police upstream drift. Exactness strategy: inline fast paths mirror only the provably-identical happy paths of `Variable._resolve_lookup`/`render_value_in_context`; any deviation (lookup failure, callable, filters, unusual types) re-renders through the *original node*, so slow paths are Django's own code. Compiler exceptions fail open to fallback (`logger "dtc"`); debug engines never compile (Django's debug page needs its interpreted render path).

Known-hard problem baked into this design: compiled code that uses fast local variables must still expose live, mutable context state to bridged third-party `Node.render(context)` calls (context escape hatch). Keep this in mind when designing codegen.

## Testing policy

The compatibility oracle is differential testing: `tests/test_backend.py` and `tests/test_compiler.py` render each case through both dtc and Django's stock backend and assert identical output. Differential tests for compiled features must also assert `template._compiled is not None` — otherwise they silently compare Django with Django (and the test settings must keep `DEBUG=False`, since debug engines never compile). `benchmarks/bench.py` measures speedup per scenario; run it when touching codegen. Any new compiler capability needs differential cases covering it, including edge semantics (autoescape/`mark_safe`, silent `VariableDoesNotExist`, context push/pop scoping, `forloop`/`parentloop`, `{{ block.super }}`). The long-term goal is to run Django's own `template_tests` suite against this engine.
