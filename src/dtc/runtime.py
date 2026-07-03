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
}

# Template._render as shipped by Django, captured before anything patches
# it. If someone has replaced it — Django's test instrumentation
# (setup_test_environment installs instrumented_test_render, which sends the
# template_rendered signal that assertTemplateUsed depends on), dtc's own
# autopatch, or any third-party hook — the compiled shortcuts below must
# route through the patched machinery instead of around it.
_pristine_render = Template._render


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
