# Roadmap

Originally the build plan; now revised to record what was actually built,
including where the design diverged from the plan. Every phase ended with
the differential suites green across the CI matrix — correctness never
waited on coverage. Phases were ordered so each one forced one big design
decision while it was still cheap to change; the biggest mid-course
revision (see phase 3) made two later phases dramatically smaller than
planned.

## Optimization principle: specialize on what the template actually uses

A fully general compiled template would not be appreciably faster than
Django's interpreter. Our structural advantage is that at compile time we
see the complete feature set a template uses, so an **analysis pass** over
the node tree decides per template (and per subtree) which generality to
strip, and codegen emits the cheapest variant that is still exactly correct.

**As shipped, the mechanism is "fast path or replay,"** which diverged from
the original sketch. The plan called for pure-locals code with *no live
`Context` object at all* when a template had no bridges, and synchronization
points at bridge boundaries otherwise. What was actually built keeps the
live `Context` maintained at all times (see phase 3 — this is what made
phases 4 and 5 small), and layers cheap reads on top of it:

- Generated fast paths inline only the *provably identical* happy paths of
  Django's own code (`Variable._resolve_lookup`, `render_value_in_context`,
  `FilterExpression.resolve`); the moment anything deviates — a lookup
  miss, a callable, an odd type — the generated code replays through the
  original node/expression, so every slow path is Django's own code.
- Reads get progressively cheaper where analysis proves it safe: scope
  locals for loop/`with`-bound names, an immutable flattened snapshot for
  names the template never writes (phase 8).
- Bookkeeping is elided where analysis proves it unobservable: the
  `forloop` dict, `enumerate`, per-iteration counter writes.
- Constant folding covers text, string literals, and constant filter
  *arguments* — never filter *calls* (a filter may be impure; `|random`
  must stay per-render), and static text is not pre-escaped (text nodes
  render verbatim in Django; string literals fold because the parser marks
  them safe). Both are deliberate narrowings of the original bullet.
- Autoescape/localization costs are skipped by *runtime type* fast paths
  (exact `str`/`SafeString`/`int` dispatch), not by static safety proofs as
  originally sketched — simpler, and exact by construction.

Every specialization carries differential tests proving the specialized and
general paths render identically, and the benchmark suite includes a
bridge-heavy worst case: tiering is also what keeps dtc from being *slower*
than stock when generality is genuinely needed (floor measured at ~1.0x).

## Phase 0 — Skeleton ✅

Backend, compiler stub with whole-template fallback, differential test
harness, CI matrix, PyPI-ready packaging.

## Phase 1 — Foundations: text + variables ✅

Templates of only `TextNode`/`VariableNode` compiled; everything else fell
back whole-template.

- **Decision made:** codegen container — generated Python source compiled
  to a module-level function replacing `Template._render`, with the backend
  proxy reproducing `Template.render`'s `push_state`/`bind_template`
  bookkeeping. And the exactness strategy: inline fast paths for the happy
  path of `Variable._resolve_lookup` (dict → attribute, Django's own
  subscriptability guard, the `silent_variable_failure` catch-all), bailing
  to a replay through the original node on any deviation. *Divergence:*
  the planned "tiered context representation with an escape hatch designed
  up front" was not built here — the escape-hatch problem was dissolved by
  the phase 3 scoping decision instead, and locals arrived in phase 8.
- Infrastructure that carried the whole project: Django's own
  `template_tests` suite wired into CI (via `dtc.autopatch` +
  `scripts/run_django_suite.py`) across Django 4.2/5.1/5.2, and the
  benchmark harness. The suite caught a real semantics bug
  (`silent_variable_failure`) within minutes of being wired.
- Policy: debug engines never compile (Django's debug error page needs the
  interpreted render path). This held through phase 7 and became the sole
  documented fallback category.
- Measured then: ~1.6–1.7x on variable-heavy templates.

## Phase 2 — Filters ✅

Full `FilterExpression` semantics; registered filter functions called
directly, never reimplemented — so **custom filters compile natively**.
The behavior flags (`is_safe`/`needs_autoescape`/`expects_localtime`) are
constant per function, so codegen reads them once at compile time and emits
only what each filter needs. Plain-str constant args fold; lazy `_("...")`
constants keep translating per render (folding would freeze the language);
variable args resolve through Django's own `Variable.resolve`.
`{% load %}` compiles away. Measured: 1.66x light chains; ~1.15x when
heavyweight filter bodies dominate (Amdahl — the filter work itself is
unchanged, as it must be).

## Phase 3 — Control flow and scoping ✅

`{% if %}`, `{% for %}` (mirroring `ForNode.render` line by line, quirks
included: the `{% empty %}` branch renders inside the pushed scope, the
per-iteration unpack pop is not exception-protected, the unpack error
message keeps its trailing space), `{% with %}`, `{% autoescape %}`,
`{% comment %}`, `{% verbatim %}`. Codegen restructured into a recursive
visitor.

- **The load-bearing decision of the whole project:** compiled code
  performs *real* context operations — `push()`/`pop()`, real
  `context[var] = item` writes — exactly as Django's nodes do. This kept
  slow-path replays, filter arguments, and cross-template rendering exact
  with zero synchronization machinery, and it collapsed the "context
  escape hatch," flagged on day one as the hardest problem, into a
  non-problem: with a live context always maintained, *any* node can be
  bridged exactly (which is what made phase 5 small).
- `forloop` maintenance (all six dict writes plus `enumerate`) is elided
  when analysis proves nothing references it — exact because the reference
  set of a fully-compiled template is statically complete.
- *Divergence:* the plan said if-conditions "compile to native Python
  boolean expressions." Only bare-variable conditions got the inline fast
  path; operator expressions evaluate through Django's own parsed condition
  objects, because smart-if operator nodes swallow exceptions
  *per-operator* — semantics that belong to Django's code, not to a
  reimplementation.
- Measured then: 1.9x simple loops, 2.1x with/if scopes;
  `{{ forloop.counter }}` loops stuck at ~1.2x, diagnosed as
  number-localization cost (fixed in phase 8).

## Phase 4 — Inheritance and inclusion ✅

`{% block %}`, `{% extends %}` (including `{% extends var %}`),
`{{ block.super }}`, `{% include %}` — via the runtime block-chain design
settled before phase 0: **no Python class inheritance**. Block bodies
compile to standalone functions attached to the original `BlockNode`s as
`_dtc_body`; the chain links at render time through Django's own
`BlockContext`, so mixed compiled/interpreted chains work in both
directions (interpreted `BlockNode.render` simply ignores the attribute).
`src/dtc/runtime.py` mirrors `BlockNode`/`ExtendsNode`/`IncludeNode.render`
(verified byte-identical across 4.2–5.2; the CI suites police drift) with
the template-render call made compiled-aware, so chains compile on demand
in plain backend mode. `{{ block.super }}` needed zero new code — the
lookup fast path bails on callables and replays through Django's machinery.

- Bug found by the benchmark: under non-cached loaders every render loaded
  fresh template instances and dtc recompiled each one (3x *slower* than
  stock). Fixed with a per-(engine, name, source) compile cache.
- Measured: include-heavy inheritance ~1.15x, bounded by Django's
  per-include protocol, which we reproduce (later addressed by the
  phase-8 literal-include fast path).

## Phase 5 — The bridge: per-node fallback ✅

*As planned*, this replaced whole-template fallback with per-node fallback
and was the coverage cliff: **every parseable template compiles** (debug
engines excepted). *As revised by phase 3*, it did not involve an escape
hatch at all: unknown nodes bridge as `node.render_annotated(context)`
against the live context — already exact for any node, including
third-party tags that mutate the context (differential-tested with one).
`render_annotated` (not `render`) honors third-party overrides and matches
`NodeList.render` in non-debug engines.

- `@simple_tag` compiles to a direct function call (argument resolution
  inlined, constants folded, `takes_context`/`target_var`/autoescape
  decided at compile time; kwargs via dict-splat since `**kwargs` tags
  accept keyword-named arguments like `class`). `@inclusion_tag` gets the
  mirror-with-compiled-render treatment when its filename form is
  compile-time known — which also sidesteps the one line of
  `InclusionNode.render` that differs between 4.2 and 5.2.
- Analysis force-rules: bridged nodes, blocks, includes, and
  `takes_context` tags inside a loop disable `forloop` elision —
  `IfChangedNode` uses the forloop dict itself as its state frame, so the
  dict is a loop-scope marker, not just a variable.
- The oracle caught a second compile-cache bug: functions embedding
  bridged nodes must not be shared across same-source template instances —
  stateful nodes key their state by node identity (Django #27974
  semantics). `__dtc_shareable__` gates the cache.
- Compiled coverage in Django's suite: ~2100 templates (everything except
  debug engines). Old whole-template worst case: 1.00x → 1.23x.

## Phase 6 — Long tail of built-ins ✅ (priorities inverted)

*Divergence:* the plan prioritized high-frequency leaf tags (`url`,
`csrf_token`, i18n). The phase 5 bridge inverted that: **leaf tags stay
bridged deliberately** — their render *is* the work (`reverse()`, storage
lookups), so dedicated codegen would save nothing measurable. **Container**
tags are where bridging hurts, because a bridged container renders its
whole subtree interpreted. Containers got codegen: `{% spaceless %}`,
`{% filter %}` (bodies compile into a sub-buffer), `{% ifchanged %}` (both
forms; identity-keyed state; in-loop forloop force), `{% localize %}`,
`{% localtime %}`, `{% timezone %}`, `{% language %}` — all verified
identical across 4.2–5.2. Still bridged, noted for later: `{% cache %}`
(its body renders only on cache miss), `{% blocktrans %}`.

- Fixed en route: all-digits lookup bits (`{{ p.0 }}`) previously failed
  the fast path and replayed the node on every access; a dedicated
  three-way step (string subscript → getattr → integer subscript, Django's
  order) took grouped-rows rendering from 0.95x to 1.6x.

## Phase 7 — The 100% milestone ✅

- Strict mode (`dtc.compiler.STRICT` / `DTC_STRICT=1`): internal compiler
  errors raise instead of falling back; the oracle suite runner always
  sets it, so a compiler bug fails CI loudly. Both suites pass strict with
  `templates_error: 0` — the fallback allowlist is exactly one category:
  debug engines.
- Test instrumentation honored: compiled shortcuts check whether
  `Template._render` is pristine and route through any patch (Django's
  `instrumented_test_render`, autopatch, third-party hooks), so
  `template_rendered`/`assertTemplateUsed` behave exactly as stock. This
  fixed a real gap — the compiled path had been bypassing instrumentation.
- Parse-time exception behavior is Django's own parser (differential tests
  document message equality).
- `tests/test_fuzz.py`: grammar-based differential fuzzer over the compiled
  feature set (fresh seed per run, reproducible via `DTC_FUZZ_SEED`); 300
  iterations per CI run; >50k iterations run clean during development.

## Phase 8 — Performance and release (optimizations ✅, release pending)

The locals work deferred since phase 1 landed here, designed once, on top
of the always-live context rather than instead of it:

- **Scope locals**: loop vars, unpacked vars, `{% with %}` bindings, and
  the forloop dict bind to Python locals (context updated in parallel).
  Static template scoping makes this sound; a scope whose body contains an
  opaque bridged tag keeps context-only reads (it could rebind names), and
  known rebinds (simple_tag `as var`) are kept in sync.
- **Exact-int output fast path**: `str(value)` unless
  `settings.USE_THOUSAND_SEPARATOR` (read per render); the phase 3
  localization bottleneck. `forloop.counter` loops: 1.17x → **5.4x**.
- **Context flattening**: units over a weighted read-score threshold take
  an immutable `context.flatten()` snapshot at function entry; reads
  partition at compile time (scope local / context walk for unit-written
  names / one `dict.get` against the snapshot, with misses and callables
  joining the slow-path replay). Any opaque bridge or `takes_context` tag
  disables it; block bodies snapshot per invocation, which is what makes
  cross-template scope reads correct. var-heavy 1.6x → 1.8x; the remaining
  per-variable cost is `escape()` itself, which both engines pay.
- **Disk cache** (`src/dtc/diskcache.py`, opt-in): marshaled code objects
  keyed by SHA-256 of the *generated source* — self-validating; codegen
  still rebuilds the namespace from the live parse, only `compile()` is
  skipped. Cold-start overhead (~9x Django's parse per template, once per
  process) cut ~70% when warm (892µs → 271µs/template). Fail-open reads,
  version-tagged directory, trusted-directory caveat documented.
- Benchmarks published in the README, with a Jinja2 reference point: dtc
  closes about half the gap to Jinja2 while staying byte-identical to
  Django — the rest is the price of Django's semantics, which Jinja2
  dropped and we keep.
- **Literal-include fast path** (the "literal-include inlining" candidate,
  built as call specialization rather than AST splicing): a
  `{% include "name" %}` site folds `construct_relative_path` at compile
  time, resolves the target and its compiled body once per top-level
  render (cached on `render_context` — the same lifetime as
  `IncludeNode.render`'s own per-node cache, so loader reload behavior
  matches stock exactly, cached loader or not), then reproduces
  `IncludeNode.render` inline per call: real `context.push()`/`new()`,
  real inlined `render_context.push_state`, direct call of the target's
  compiled function. A patched `Template._render` or uncompiled target
  routes through the runtime mirror per call; autopatch's stats-only
  `_render` registers itself in `runtime._transparent_renders` so the
  oracle suite exercises the fast path rather than always falling back.
  AST splicing was rejected deliberately: the per-include `push_state`
  frame and `context.push` are observable (ifchanged/cycle state frames,
  BlockContext isolation), so inlining could remove only the call
  overhead, not the protocol. Bonus from the same analysis: isolated
  (`only`) includes no longer force forloop maintenance in enclosing
  loops — `context.new()` provably hides it. inheritance scenario
  (extends + include in loop): 1.15x → **1.59x**.

**Remaining for 0.1.0:** trial against a real application's templates,
release.
