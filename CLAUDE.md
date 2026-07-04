# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project goal

A drop-in replacement for Django's template engine that compiles templates to Python for speed, with **100% compatibility** as a hard requirement — including third-party custom tags and filters. Output must be byte-identical to Django's stock engine in all cases. When compatibility and speed conflict, compatibility wins: anything the compiler can't handle correctly must fall back to Django's interpreted renderer rather than approximate it.

Work proceeded in phases — see `ROADMAP.md` for the full record, including where the design diverged from the plan. All eight phases' engineering is complete (scope locals, context flattening, int fast path, the disk cache, and the literal-include fast path included); what remains before 0.1.0 is release work: a trial against a real application and the PyPI release.

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
.venv/bin/python benchmarks/bench.py                         # speed vs stock engine + cold-start report
.venv/bin/python scripts/run_django_suite.py <django-src>    # Django's own template_tests vs dtc (strict)
DTC_FUZZ_ITERS=10000 .venv/bin/pytest tests/test_fuzz.py     # extended differential fuzzing
```

For the last command, `<django-src>` is a Django source checkout whose tag matches the installed version (the script verifies): `git clone --depth 1 --branch $(python -c 'import django; print(django.__version__)') https://github.com/django/django.git`. It works by installing `dtc.autopatch`, which patches `Template._render` engine-wide (and `django.test.utils.instrumented_test_render`, preserving the `template_rendered` signal) — that's what routes Django's engine-level test templates through the compiler. It prints compiled/fallback stats at exit; a run where `renders_compiled` is 0 proved nothing.

Version is single-sourced from `__version__` in `src/dtc/__init__.py` (hatchling dynamic version). CI (`.github/workflows/ci.yml`) tests Python 3.10–3.13 × Django 4.2/5.1/5.2 — code must work across that whole matrix.

## Architecture

The design is **hybrid: compile what we can, bridge what we can't.**

- Templates are parsed by Django's own `Lexer`/`Parser` (never reimplement parsing — reusing it is a deliberate compatibility decision). Only the render path is replaced.
- `src/dtc/backend.py` — `DTCTemplates`, a `BACKENDS` engine that subclasses Django's stock `DjangoTemplates` backend, inheriting all configuration/loader/OPTIONS handling unchanged (the one dtc-specific option, `dtc_disk_cache`, is popped before Django's `Engine` sees it). Its `Template` proxy renders through `runtime.template_render`, which takes the compiled path when available and otherwise Django's own `Template.render`; either way `Template.render`'s observable bookkeeping (`render_context.push_state`, `bind_template` — which runs context processors and exposes `context.template.engine`) is reproduced exactly.
- `src/dtc/compiler.py` — the compiler. Contract: `compile_template(template) -> callable(Context) -> str | None`, where the callable replaces `Template._render`. `None` now means only "debug engine" (by policy) or "internal compiler error" (fail-open, logged to logger `dtc`, counted in `stats["templates_error"]`; raises instead under `dtc.compiler.STRICT` / `DTC_STRICT=1`). **Every parseable template compiles.** Dedicated codegen covers text, variables (filters call the registered functions directly, with `is_safe`/`needs_autoescape`/`expects_localtime` specialization decided at compile time — custom filters compile natively), `{% if %}`, `{% for %}`, `{% with %}`, `{% autoescape %}`, `{% comment %}`, `{% verbatim %}`, `{% load %}`, inheritance (`{% block %}`/`{% extends %}`/`{% include %}`), `@simple_tag`, `@inclusion_tag`, and the container tags (`{% spaceless %}`, `{% filter %}`, `{% ifchanged %}`, `{% localize %}`, `{% localtime %}`, `{% timezone %}`, `{% language %}` — containers matter because bridging one forces its subtree to render interpreted); any other node bridges as `render_annotated(context)`, exact because compiled code performs *real* `context.push()/pop()/__setitem__` operations. Leaf tags (`url`, `csrf_token`, `static`, ...) stay bridged deliberately: their render is the work itself. Literal `{% include "name" %}` sites get a fast path: `construct_relative_path` folds at compile time, the target and its compiled function resolve once per top-level render (cached on `render_context`, the same lifetime as `IncludeNode.render`'s own per-node cache, so loader reload semantics match stock), and each call reproduces `IncludeNode.render` inline — real `context.push()`/`new()`, inlined `push_state` — before calling the target's compiled function directly; a patched `Template._render` (anything not in `runtime._transparent_renders`) or an uncompiled target falls back to the runtime mirror per call. Speed layers, each gated by the analysis pass: exact-type output fast paths (`str`/`SafeString`/`int`); the `forloop` dict elided when provably unreferenced (bridged nodes, blocks, non-isolated includes, and `takes_context` tags inside a loop disable elision — isolated `{% include ... only %}` renders against `context.new()` and doesn't — unseen content may reference `forloop`, and `IfChangedNode` uses the dict as its state frame); scope locals for loop/`with`-bound names (disabled per scope when its body contains an opaque bridge or a `takes_context` tag call, either of which can rebind names behind the locals); and a flattened read snapshot for names the unit never writes (disabled template-wide by any opaque bridge or `takes_context` tag, threshold-gated by weighted read count). Compiled functions embedding bridged or identity-keyed-state nodes are marked non-shareable (`__dtc_shareable__`) — stateful nodes key state by node identity, so per-parse functions must stay per-parse.
- `src/dtc/diskcache.py` — opt-in cold-start cache (`OPTIONS["dtc_disk_cache"]` / `DTC_DISK_CACHE`): marshaled code objects keyed by SHA-256 of the generated source (self-validating — codegen still runs per process to rebuild the namespace from live parse objects; only `compile()` is skipped). Fail-open on every read; loaded code is exec'd, so the directory must be trusted.
- `src/dtc/runtime.py` — render-time support: `compiled_for()` (per-instance + per-(engine, name, source) compile caching — the latter prevents recompiling per render under non-cached loaders; only `__dtc_shareable__` functions enter the source cache), the pristine-`_render` check (`_render_is_patched`) that routes around the compiled path when anything has patched `Template._render` (plus `_transparent_renders`, the list of render functions the literal-include fast path may route around — pristine `_render` and autopatch's stats-only replacement), `resolve_include` (per-render resolution for literal include sites), the `stats` counters, and mirrors of `BlockNode`/`ExtendsNode`/`IncludeNode.render` with the template-render call made compiled-aware. Block bodies compile to standalone functions attached to BlockNodes as `_dtc_body`, linked at render time through Django's own `BlockContext`, so mixed compiled/interpreted inheritance chains work in both directions. The mirrors are verified byte-identical across Django 4.2–5.2; the CI oracle suites police upstream drift.
- `src/dtc/autopatch.py` — opt-in engine-wide hook (patches `Template._render`); used by the oracle suite runner. The supported integration is the backend.

The central exactness strategy (originally the "context escape hatch" problem, dissolved in phase 3): **the live `Context` is always maintained and always authoritative** — compiled code performs real `push()/pop()/__setitem__` operations, which is what makes bridging arbitrary third-party nodes exact. Scope locals and the flattened snapshot are read-only accelerations layered on top, gated off by analysis wherever an opaque node could write behind them. The hoisted `_autoescape` local is resynced after every site that hands the live context to foreign code (bridged renders, slow-path node replays, `takes_context` calls, the block/include mirrors) — such code may set `context.autoescape`, which stock rendering reads live. Generated fast paths inline only the provably-identical happy paths of `Variable._resolve_lookup`/`render_value_in_context`/`FilterExpression.resolve`; any deviation (lookup failure, callable, unusual type) replays through the *original node*, so every slow path is Django's own code. Preserve both halves of this when changing codegen.

## Testing policy

Three oracle layers, all in CI:

1. Differential tests: `tests/test_backend.py` and `tests/test_compiler.py` render each case through both dtc and Django's stock backend and assert identical output. Differential tests for compiled features must also assert `template._compiled is not None` — otherwise they silently compare Django with Django (and the test settings must keep `DEBUG=False`, since debug engines never compile). Any new compiler capability needs differential cases covering it, including edge semantics (autoescape/`mark_safe`, silent `VariableDoesNotExist`, context push/pop scoping, `forloop`/`parentloop`, `{{ block.super }}`).
2. Django's own `template_tests` suite runs against dtc in CI on every supported Django version, in strict mode, via `scripts/run_django_suite.py` — it has caught real bugs every time coverage grew. The stats it prints must show a large `renders_compiled` and `templates_error: 0`.
3. `tests/test_fuzz.py` — grammar-based differential fuzzer, fresh seed per run (`DTC_FUZZ_SEED=<printed seed>` to reproduce, `DTC_FUZZ_ITERS` to scale up; run ≥10k iterations locally after codegen changes).

`benchmarks/bench.py` measures speedup per scenario plus a cold-start report (parse vs. compile vs. disk-cache-warm); run it when touching codegen — it has caught performance regressions the tests can't (recompile-per-render, fast-path misses).
