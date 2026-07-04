"""Render-time support for compiled templates.

The three node helpers here mirror ``BlockNode.render``,
``ExtendsNode.render``, and ``IncludeNode.render`` line by line (bodies
verified byte-identical across Django 4.2–5.2), with the single
template/nodelist rendering call swapped for a compiled-aware equivalent.
Running Django's own template_tests suite on every supported version in CI
is what polices these mirrors against upstream drift.

Interop contract (the "mixed chains" requirement): everything stored in
``BlockContext`` is a real ``BlockNode``. Compiled block bodies ride along
as a ``_dtc_body`` attribute on the original node; Django's interpreted
``BlockNode.render`` ignores it (rendering the same nodelist, just
interpreted), while ``render_block`` below prefers it. Either side of an
inheritance chain can be compiled or interpreted in any combination.
"""

from __future__ import annotations

import weakref

from django.template.base import Template, TextNode
from django.template.loader_tags import (
    BLOCK_CONTEXT_KEY,
    BlockContext,
    ExtendsNode,
    construct_relative_path,
)

_MISSING = object()

#: Compile/render counters (autopatch adds render counts): lets oracle-suite
#: runs prove the compiled path was actually exercised. templates_error
#: counts fail-open compiler errors — anything nonzero is a dtc bug.
stats = {
    "templates_compiled": 0,
    "templates_fallback": 0,
    "templates_error": 0,
    "renders_compiled": 0,
    "renders_fallback": 0,
    "disk_hits": 0,
    "disk_misses": 0,
}

# Template._render as shipped by Django, captured before anything patches
# it. If someone has replaced it — Django's test instrumentation
# (setup_test_environment installs instrumented_test_render, which sends the
# template_rendered signal that assertTemplateUsed depends on), dtc's own
# autopatch, or any third-party hook — the compiled shortcuts below must
# route through the patched machinery instead of around it.
_pristine_render = Template._render

# Render functions the compiled include fast path may route around without
# losing behavior: Django's own _render, plus autopatch's replacement (it
# only adds stats counting; install() appends it here). The substituted
# instrumented_test_render is deliberately NOT in this list — it sends the
# template_rendered signal, which must keep firing per included template.
# A list, not a tuple: compiled functions capture the object itself, so
# autopatch installing after templates have compiled is still seen.
_transparent_renders = [_pristine_render]


def _render_is_patched(template):
    return type(template)._render is not _pristine_render


# engine -> {(template name, source): compiled fn or None}. Engines without
# a cached loader hand out a *fresh* Template instance per load; without this
# second-level cache every {% extends %}/{% include %} target would recompile
# on every render (measured 3x slower than stock, vs the intended speedup).
# Keyed by name+source per engine; from_string templates (no loader name)
# skip it to avoid unbounded growth on user-generated template strings.
# Only functions marked __dtc_shareable__ are stored: a function embedding
# bridged nodes must stay per-parse, because Django gives each parse its own
# node instances and stateful nodes (IfChangedNode, CycleNode) key state by
# node identity — sharing would merge state Django keeps separate (caught by
# template_tests.test_if_changed.test_include_state).
_source_cache = weakref.WeakKeyDictionary()


def compiled_for(template):
    """The compiled render callable for *template*, or None; cached on the
    template instance, and per (engine, name, source) across instances."""
    compiled = template.__dict__.get("_dtc_compiled", _MISSING)
    if compiled is not _MISSING:
        return compiled

    origin_name = getattr(template.origin, "template_name", None)
    engine_cache = None
    if origin_name is not None:
        engine_cache = _source_cache.setdefault(template.engine, {})
        compiled = engine_cache.get((origin_name, template.source), _MISSING)
        if compiled is not _MISSING:
            template._dtc_compiled = compiled
            return compiled

    from .compiler import compile_template  # avoid import cycle

    compiled = compile_template(template)  # fail-open: None on error
    template._dtc_compiled = compiled
    if engine_cache is not None and getattr(compiled, "__dtc_shareable__", True):
        engine_cache[(origin_name, template.source)] = compiled
    key = "templates_compiled" if compiled is not None else "templates_fallback"
    stats[key] += 1
    return compiled


def render_body(template, context):
    """``Template._render(context)``, compiled when possible. Used where
    Django deliberately calls ``_render`` (ExtendsNode: parent shares the
    child's bound context)."""
    if _render_is_patched(template):
        return template._render(context)
    fn = compiled_for(template)
    if fn is None:
        return template._render(context)
    return fn(context)


def template_render(template, context):
    """``django.template.base.Template.render(context)``, compiled when
    possible: reproduces render()'s push_state/bind_template bookkeeping
    around the compiled body."""
    if not isinstance(template, Template):
        # e.g. {% include template_object %} where the object came from a
        # foreign engine: exactly what Django would call.
        return template.render(context)
    if _render_is_patched(template):
        # Test instrumentation (template_rendered signal), autopatch, or a
        # third-party hook owns _render: go through Template.render so the
        # patch runs exactly as it would under stock Django.
        return template.render(context)
    fn = compiled_for(template)
    if fn is None:
        return template.render(context)
    with context.render_context.push_state(template):
        if context.template is None:
            with context.bind_template(template):
                context.template_name = template.name
                return fn(context)
        else:
            return fn(context)


def render_block(node, context):
    """Mirror of BlockNode.render. The effective block (an override popped
    from BlockContext, or this node) renders through its compiled body when
    its template was compiled; the object exposed as ``{{ block }}`` is a
    plain BlockNode copy, so ``{{ block.super }}`` behaves exactly as
    stock (walking the chain through Django's own machinery)."""
    block_context = context.render_context.get(BLOCK_CONTEXT_KEY)
    with context.push():
        if block_context is None:
            context["block"] = node
            body = node.__dict__.get("_dtc_body")
            result = (
                body(context) if body is not None else node.nodelist.render(context)
            )
        else:
            push = source = block_context.pop(node.name)
            if source is None:
                source = node
            block = type(node)(source.name, source.nodelist)
            block.context = context
            context["block"] = block
            body = source.__dict__.get("_dtc_body")
            result = (
                body(context) if body is not None else block.nodelist.render(context)
            )
            if push is not None:
                block_context.push(node.name, push)
    return result


def render_extends(node, context):
    """Mirror of ExtendsNode.render: parent resolution stays Django's own
    (node.get_parent handles {% extends var %}, backend template objects,
    and loader history for recursive extends); the parent body renders
    compiled when possible."""
    compiled_parent = node.get_parent(context)

    if BLOCK_CONTEXT_KEY not in context.render_context:
        context.render_context[BLOCK_CONTEXT_KEY] = BlockContext()
    block_context = context.render_context[BLOCK_CONTEXT_KEY]

    block_context.add_blocks(node.blocks)

    for n in compiled_parent.nodelist:
        # The ExtendsNode has to be the first non-text node.
        if not isinstance(n, TextNode):
            if not isinstance(n, ExtendsNode):
                from django.template.loader_tags import BlockNode

                blocks = {
                    bn.name: bn
                    for bn in compiled_parent.nodelist.get_nodes_by_type(BlockNode)
                }
                block_context.add_blocks(blocks)
            break

    with context.render_context.push_state(compiled_parent, isolated_context=False):
        return render_body(compiled_parent, context)


def flatten_tail(context):
    """``Context.flatten()`` minus the root layer (``dicts[0]``). The flat
    read snapshot deliberately excludes it: a root write —
    ``context.dicts[0][k] = v``, the pattern tags use to persist a value
    across template boundaries, outliving every scope pop — must
    never be served stale from a snapshot taken before the write. Excluded,
    root-resolved names miss the snapshot and replay through the live
    context walk, which is always fresh. Nothing legitimate is lost: the
    root layer holds only the context builtins, and ``True``/``False``/
    ``None`` parse as literals, never as variable lookups."""
    flat = {}
    for d in context.dicts[1:]:
        flat.update(d)
    return flat


def _context_snapshot(context):
    return [dict(d) for d in context.dicts]


def _diff_layers(label, before, context, allowed=frozenset()):
    """Raise ContextSafeViolation if any context layer changed beyond the
    declared *allowed* keys, naming the offending keys in the first layer
    that did. Declared writes mean "may set": a removed key always violates
    (deleting can change what an outer-scope read resolves to, which the
    resynced scope locals don't model)."""
    from . import ContextSafeViolation

    for i, (was, now) in enumerate(zip(before, context.dicts)):
        now = dict(now)
        if now == was:
            continue
        added = sorted(k for k in now if k not in was and k not in allowed)
        removed = sorted(k for k in was if k not in now)
        changed = sorted(
            k
            for k in now
            if k in was
            and k not in allowed
            and now[k] is not was[k]
            and now[k] != was[k]
        )
        if not (added or removed or changed):
            continue
        declared = f" (declared writes: {sorted(allowed)!r})" if allowed else ""
        raise ContextSafeViolation(
            f"{label} is declared context-safe{declared} but mutated context "
            f"layer {i} beyond its declaration (contract clause (a)): "
            f"added={added!r} removed={removed!r} changed={changed!r}"
        )


def checked_safe_render(node, context, allowed=frozenset()):
    """DTC_CHECK_DECLARATIONS bridge for a node declared
    ``dtc_context_safe`` or ``dtc_context_writes``: verifies contract
    clauses (a) — no context writes beyond the declared *allowed* keys —
    and (b) — no state keyed by node identity — on every call. Clause (c),
    source-determinism, is not mechanically checkable, nor is an unlisted
    child nodelist (clause (d)). TagHelperNodes are exempt from the
    identity check: their declaration rides on the tag function (only
    clause (a) applies), and InclusionNode's own per-render template cache
    is Django's blessed machinery."""
    from . import ContextSafeViolation

    label = f"{type(node).__module__}.{type(node).__qualname__}"
    depth_before = len(context.dicts)
    before = _context_snapshot(context)
    result = node.render_annotated(context)
    if len(context.dicts) != depth_before:
        raise ContextSafeViolation(
            f"{label} is declared context-safe but changed the context "
            f"stack depth from {depth_before} to {len(context.dicts)} "
            "(unbalanced push/pop violates contract clause (a))"
        )
    _diff_layers(label, before, context, allowed)
    from django.template.library import TagHelperNode

    if not isinstance(node, TagHelperNode) and any(
        node in d for d in context.render_context.dicts
    ):
        raise ContextSafeViolation(
            f"{label} is declared dtc_context_safe but keeps render_context "
            "state keyed by its own identity (contract clause (b)); it must "
            "not be declared safe"
        )
    return result


def checked_safe_call(func, context, thunk):
    """DTC_CHECK_DECLARATIONS wrapper for a ``takes_context`` tag function
    declared ``dtc_context_safe``: verifies contract clause (a) around the
    call built by the compiler (``thunk``)."""
    from . import ContextSafeViolation

    label = f"{func.__module__}.{func.__qualname__}"
    depth_before = len(context.dicts)
    before = _context_snapshot(context)
    result = thunk()
    if len(context.dicts) != depth_before:
        raise ContextSafeViolation(
            f"{label} is declared dtc_context_safe but changed the context "
            f"stack depth from {depth_before} to {len(context.dicts)} "
            "(unbalanced push/pop violates contract clause (a))"
        )
    _diff_layers(label, before, context)
    return result


def resolve_include(key, names, context):
    """First render-time use of a literal ``{% include %}`` site: resolve
    the target exactly as ``IncludeNode.render`` would (same engine, same
    ``select_template`` call) and pair it with its compiled body. The pair
    is cached on ``render_context`` under the site's *key* for the rest of
    this render — the same lifetime as ``IncludeNode.render``'s own
    per-node cache — so template reloading behaves identically to stock
    under any loader, cached or not. A failed lookup is not cached: like
    stock, every render retries and raises ``TemplateDoesNotExist``."""
    template = context.template.engine.select_template(names)
    pair = (template, compiled_for(template))
    context.render_context.dicts[0][key] = pair
    return pair


def render_include(node, context):
    """Mirror of IncludeNode.render, including its per-node template cache
    in render_context; the included template renders compiled when
    possible."""
    template = node.template.resolve(context)
    # Does this quack like a Template?
    if not callable(getattr(template, "render", None)):
        # If not, try the cache and select_template().
        template_name = template or ()
        if isinstance(template_name, str):
            template_name = (
                construct_relative_path(
                    node.origin.template_name,
                    template_name,
                ),
            )
        else:
            template_name = tuple(template_name)
        cache = context.render_context.dicts[0].setdefault(node, {})
        template = cache.get(template_name)
        if template is None:
            template = context.template.engine.select_template(template_name)
            cache[template_name] = template
    # Use the base.Template of a backends.django.Template.
    elif hasattr(template, "template"):
        template = template.template
    values = {name: var.resolve(context) for name, var in node.extra_context.items()}
    if node.isolated_context:
        return template_render(template, context.new(values))
    with context.push(**values):
        return template_render(template, context)
