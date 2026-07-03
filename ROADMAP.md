# Roadmap

Each phase ends with the differential suite green across the CI matrix. A
template (or node) the current phase can't compile falls back — correctness
never waits on coverage. Phases are ordered so each one forces one big design
decision while it's still cheap to change.

## Optimization principle: specialize on what the template actually uses

A fully general compiled template — one that maintains a live `Context` and
supports every feature at every point — would not be appreciably faster than
Django's interpreter. Our structural advantage is that at compile time we see
the complete feature set a template uses, so codegen runs behind an **analysis
pass** over the node tree that computes a per-template (and per-subtree)
capability profile, and emits the cheapest variant that is still exactly
correct:

- **No bridged nodes anywhere** → pure local-variable code; no live `Context`
  object is maintained at all. Templates with bridges pay for context
  synchronization only at bridge boundaries (sync in, `node.render(context)`,
  sync out) — everything between bridges still runs on locals.
- **`forloop` never referenced** in a loop body → emit a bare Python loop; no
  forloop dict, no counter bookkeeping. Same for `parentloop`,
  `revcounter`/`last` (which need `len()`), and `{{ block.super }}` chains.
- **Constant folding**: literal-only expressions and filter calls with
  constant arguments evaluate at compile time; static text runs are
  pre-concatenated and pre-escaped.
- **Autoescape analysis**: skip `conditional_escape` where output is provably
  safe (literals, filters marked `is_safe` fed safe input).

Every specialization needs a differential test proving the specialized and
general paths render identically. The benchmark suite must include a
bridge-heavy worst case: tiering is also what keeps dtc from being *slower*
than stock when generality is genuinely needed.

## Phase 0 — Skeleton ✅

Backend, compiler stub with whole-template fallback, differential test
harness, CI matrix, PyPI-ready packaging.

## Phase 1 — Foundations: text + variables (core ✅)

Compile templates consisting of `TextNode` and `VariableNode` only;
everything else still falls back whole-template.

Status: **complete.** Codegen, tiered fast paths, differential suite,
benchmark harness, debug-mode policy, and Django's own `template_tests`
suite wired into CI across Django 4.2/5.1/5.2 (via `dtc.autopatch` +
`scripts/run_django_suite.py`) — passing, with ~560 templates taking the
compiled path. Measured: ~1.6–1.7x on variable-heavy templates, ~1.0x worst
case (bridged filters).

- **Decision forced:** codegen container (generated Python source compiled to
  a module-level render function) and the context representation — how
  compiled code reads/writes context state. The representation must be
  **tiered from day one**: the analysis pass decides whether a template gets
  the pure-locals fast path or carries context sync machinery. Design the
  escape hatch now even though nothing uses it yet.
- Variable resolution must match `Variable._resolve_lookup` exactly: dict key
  → attribute → numeric index, callables (`alters_data`,
  `do_not_call_in_templates`), silent `VariableDoesNotExist`,
  `string_if_invalid`, lazy objects, autoescape/`mark_safe`.
- **Infrastructure (in parallel):**
  - Wire Django's own `template_tests` suite to run against dtc in CI. It
    passes trivially today (everything falls back) and polices every phase
    after.
  - Benchmark harness (dtc vs. stock engine on representative templates) so
    every phase proves its speed claim. If phase 1 isn't clearly faster on
    variable-heavy templates, revisit the design before building more.
- Policy decision: when `debug=True`, don't compile (Django's debug error
  page needs its own render internals). Revisit in phase 7.

## Phase 2 — Filters ✅

Full `FilterExpression` semantics: filter arguments (literal and variable),
`is_safe`/`needs_autoescape`/`expects_localtime`, safe-string propagation.
Call Django's registered filter functions directly — never reimplement them.
Consequence worth advertising: **custom filters compile natively** from this
phase on, since a filter is just a registered callable.

Status: **complete.** The behavior flags are read at compile time (they're
constant per function) and codegen specializes on them; plain-str constant
args fold, lazy i18n constants keep translating per render; `{% load %}`
compiles away. Both oracle suites pass. Measured: 1.66x on light filter
chains; ~1.2x when heavyweight filter bodies dominate (Amdahl's law — the
filter work itself is unchanged, as it must be).

## Phase 3 — Control flow and scoping

`{% if %}` (full smart-if operator grammar compiled to Python expressions,
errors-mean-False semantics), `{% for %}` (real Python loop; `forloop` with
`parentloop`, unpacking, `reversed`, `{% empty %}`), `{% with %}`, plus
trivial tags (`{% comment %}`, `{% verbatim %}`, `{% firstof %}`,
`{% templatetag %}`, `{% autoescape %}`).

- Specializations landing here: bare Python loops when `forloop` is never
  referenced; `parentloop`/`revcounter`/`last` bookkeeping only when used;
  `{% if %}` conditions compile to native Python boolean expressions.

- **Decision forced:** how compiled code models context push/pop scoping
  (Django pushes a scope per loop iteration and per `with`) while staying
  cheap. This design must anticipate the phase-5 escape hatch.

## Phase 4 — Inheritance and inclusion

`{% block %}`, `{% extends %}` (including `{% extends var %}`),
`{{ block.super }}`, `{% include %}` (variable names, `with`/`only`).

- Blocks compile to standalone functions; the chain links at render time with
  BlockContext-compatible per-name stacks — no Python class inheritance (see
  memory/design notes). Parent lookup goes through the loaders per render.
- Mixed chains must work: a compiled child extending an interpreted parent
  and vice versa. This is what makes per-template fallback composable.

## Phase 5 — The bridge: per-node fallback

Replace whole-template fallback with per-node fallback: an unknown `Node`
compiles to a call into its original `node.render(context)` with live context
state (the escape hatch — context mutations by third-party tags must be
visible to compiled code and vice versa).

- This is where the tiered context representation pays off: templates with no
  bridged nodes keep the pure-locals path untouched; sync costs are confined
  to bridge boundaries in templates that have them.

- Fast paths for `@simple_tag` / `@inclusion_tag`, which are declarative
  enough to compile directly and cover most real-world custom tags.
- **Exit criterion: every parseable template compiles** — fallback granularity
  is now a node, not a template. This is the coverage cliff; after phase 5,
  real apps run mostly-compiled.

## Phase 6 — Long tail of built-ins

Dedicated codegen (or verified bridging where codegen isn't worth it) for the
stateful and odd tags: `{% cycle %}` (state across loop iterations, `silent`),
`{% ifchanged %}`, `{% regroup %}`, `{% url %}`, `{% now %}`, `{% lorem %}`,
`{% widthratio %}`, `{% filter %}`, `{% csrf_token %}`, `{% cache %}`,
`{% spaceless %}`, i18n (`{% trans %}`/`{% blocktrans %}`), l10n, tz.
Prioritize by frequency in real codebases: `url`, `csrf_token`, and i18n
carry most production templates.

## Phase 7 — The 100% milestone

- Django's full `template_tests` suite passes with **fallback disabled**
  (compilation forced) except for a documented allowlist of
  designed-to-bridge cases.
- Debug/tooling compatibility: `template_rendered` signal for the test
  client, `TemplateSyntaxError`/`VariableDoesNotExist` timing, exception
  behavior mapping back to template source, decide the final `debug=True`
  story.
- Differential fuzzer as a second oracle layer.

## Phase 8 — Performance and release

- Optimization passes informed by profiling: context flattening, per-engine
  compiled-template cache, optional disk cache of generated modules (cold
  starts).
- Published benchmarks (vs. stock engine; Jinja2 as reference point).
- Docs, 0.1.0 on PyPI, trial in a real application.
