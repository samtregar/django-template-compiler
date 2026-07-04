# django-template-compiler

A drop-in replacement for Django's template engine, 100% compatible including custom tags and filters, but much faster.

**Status: pre-alpha, but substantially complete.** Every parseable template compiles: dedicated code generation for the core template language (variables, filters, control flow, inheritance, `simple_tag`/`inclusion_tag`, container tags), with anything else — arbitrary third-party tags included — running as-is against the live context. dtc passes Django's own template test suite (Django 4.2–5.2) in CI, plus a differential fuzzer. Typical speedups: 2.5–4.5x on template-bound rendering, with a ~1.2x floor when a template is dominated by bridged tags. Not yet exercised by production traffic — try it and report.

Two behaviors worth knowing:

- **`DEBUG=True` disables compilation** (per engine): Django's debug error page and exception annotation need the interpreted render path. Production configs get the compiled path; development keeps perfect debugging.
- **Django test instrumentation is honored**: when `setup_test_environment()` (the test runner / `assertTemplateUsed`) patches template rendering, dtc detects the patch and routes through it, so the `template_rendered` signal fires exactly as with stock Django.

## How it works

Templates are parsed with Django's own lexer and parser, then compiled to Python code — a `{% for %}` loop becomes a real Python `for` loop, variable lookups become direct attribute/key access. Anything the compiler can't handle yet (including arbitrary custom tags) falls back to Django's interpreted render path, so output is always exactly what Django would produce.

## Benchmarks

`benchmarks/bench.py`, Python 3.11, Django 5.2 (µs per render; higher speedup is better):

| scenario | django | dtc | speedup |
|---|---:|---:|---:|
| 40 plain variables | 56.7 | 15.4 | 3.7x |
| 100-row loop | 174.4 | 38.4 | 4.5x |
| 100-row loop with `forloop.counter` | 717.6 | 110.0 | **6.5x** |
| 50×4 table (nested loop + if) | 542.8 | 208.5 | 2.6x |
| with/if scopes | 222.0 | 68.7 | 3.2x |
| spaceless-wrapped table | 273.0 | 78.5 | 3.5x |
| inheritance + include in loop | 175.8 | 95.5 | 1.8x |
| bridged unknown tag (worst case) | 26.5 | 18.0 | 1.5x |

For reference, Jinja2 renders the table scenario in ~85µs — dtc closes about three-quarters of the gap to Jinja2 while producing byte-identical Django output. The remaining distance is the price of Django's semantics themselves (silent variable failures, callable auto-invocation, the context stack), which dtc preserves exactly and Jinja2 deliberately dropped.

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

### Declaring custom tags context-safe

A custom tag without dedicated codegen renders through its own `render()` against the live context, which is always exact — but because the compiler can't see what that `render()` does, one such tag disables the read optimizations around it: the flattened read snapshot (template-wide), scope locals (in every enclosing `{% for %}`/`{% with %}`), and compiled-function sharing across template instances (which matters without a cached loader). `takes_context` simple/inclusion tags pay the first two as well.

Most tags never write the context. If yours is one of them, declare it:

```python
class BreadcrumbNode(Node):
    dtc_context_safe = True   # stock Django ignores this; dtc keeps its
    ...                       # optimizations around the tag

# takes_context tags declare the *function*:
@register.simple_tag(takes_context=True)
def current_section(context):
    return context.get("section", "home")
current_section.dtc_context_safe = True

# third-party tags you can't edit, e.g. in settings or AppConfig.ready():
import dtc
dtc.declare_safe(SomeThirdPartyNode)
```

The declaration is a promise about every `render()` call: the context stack and its mappings are left exactly as found (balanced push/pop inside is fine); no state keyed on the node's identity (Django's `CycleNode`/`IfChangedNode` pattern); behavior depends only on the parsed source. *Reading* the context is always fine, as is setting `context.autoescape`. A container tag may render nested writers freely, provided every nodelist it renders is listed in the standard `child_nodelists` attribute — the compiler analyzes those children itself; a rendered-but-unlisted nodelist is the one thing that can silently break output. Subclasses inherit the declaration with the `render()` it describes (`dtc_context_safe = False` opts back out). See `help(dtc.declare_safe)` for the precise contract.

Tags that *do* write the context can declare **what** they write instead, as long as the target names are fixed at parse time — the common capture/setter shape:

```python
class CaptureNode(Node):
    # names the instance attributes holding the written context keys
    dtc_context_writes = ("target",)

    def __init__(self, nodelist, target):
        self.nodelist = nodelist
        self.target = target            # {% capture NAME %}...{% endcapture %}

    def render(self, context):
        context[self.target] = self.nodelist.render(context)
        return ""

# or for classes you can't edit:
dtc.declare_writes(SomeVendorSetterNode, "dest")
```

The compiler routes reads of the declared names through the live context and keeps every optimization on for everything else — including scope locals: if a declared write shadows a `{% for %}`/`{% with %}` name, the generated code re-reads that local right after the tag runs. The rest of the contract matches `dtc_context_safe`; the declared keys may be *set* only (no deletions), and an attribute holding `None` means an optional target unused at that site. See `help(dtc.declare_writes)`.

Declared writes may target the normal top-of-stack (`context[key] = value`) **or** the root layer (`context.dicts[0][key] = value` — the pattern used by tags that persist a value across template boundaries, past every scope pop). Root-written names are never served from the read snapshot (it excludes the root layer by design), so they stay exact across template boundaries, includes, and re-writes.

A wrong declaration produces wrong output silently — so verify it: run your test suite with `DTC_CHECK_DECLARATIONS=1` and dtc checks every declared render, raising `dtc.ContextSafeViolation` on any write outside the declaration. (Containers wrapping legitimate writers are skipped by the checker; the source-determinism clause isn't mechanically checkable.)

Tags that just compute a value from their arguments — a formatter, a calculator, a lookup — are better rewritten as `@register.simple_tag`: those compile natively, declaration-free, with argument resolution inlined.

### Limitation: tags that rewrite enclosing context layers

Within a single template, *any* custom tag is rendered exactly — the compiler disables its read optimizations around every tag it doesn't recognize. Across template boundaries there is one assumption: a tag's context effects that outlive an `{% include %}`/`{% block %}`/`{% extends %}` are either **scope-limited** (ordinary `context[key] = value` writes and balanced push/pop, which die with the layers that the include/block machinery pops) or **root-layer** (`context.dicts[0][key] = value`, which dtc handles as described above). Every Django built-in and every `simple_tag`/`inclusion_tag` satisfies this.

A tag that mutates an *intermediate* layer of the caller's stack — indexing `context.dicts[1]`, calling `Context.set_upward()`, deleting keys from enclosing layers, or leaking an unbalanced `push()` — from inside an included or extended template **can produce output that differs from stock Django**: the enclosing template was compiled without knowledge of that tag, and its read snapshot or scope locals may serve the pre-mutation value. `DTC_CHECK_DECLARATIONS` cannot catch this (the tag carries no declaration, and the effect surfaces in a different template than the tag).

If you have such a tag, the supported paths are: write the root layer instead (`dicts[0]` — fully supported and declarable), write the top of the stack, or confine the mutation to the template that renders the tag. Note that intermediate-layer writes are fragile under stock Django too — what `dicts[1]` *is* depends on the stack depth at the call site.

### Limitation: `override_settings` entered mid-render

Compiled code reads `USE_THOUSAND_SEPARATOR` (which controls `{{ int }}` formatting) through the concrete settings object behind the `django.conf.settings` proxy, re-resolved at the start of every render and after every bridged tag, `takes_context` call, and slow-path replay. Assigning a setting directly (`settings.USE_THOUSAND_SEPARATOR = True`), from anywhere — even a filter — writes through to that object and is observed by the very next output, exactly like stock. The one mutation that isn't: calling `override_settings().enable()` from a *filter* or a non-`takes_context` tag and leaving it active across the call boundary. That swaps the object behind the proxy, and compiled int outputs keep reading the old one until the next bridged-tag/`takes_context`/replay site, where stock Django observes the swap immediately. `override_settings` used as intended — wrapped around a render, in tests — is always exact, as is entering/exiting it from a bridged (unrecognized) tag.

## Development

```bash
pip install -e .[dev]
pytest
```

## License

BSD 3-Clause. See [LICENSE](LICENSE).
