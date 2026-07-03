"""Compile parsed Django templates to Python.

Phase 1: templates consisting solely of ``TextNode`` and ``VariableNode``
compile to a generated Python function; anything else returns ``None`` and
the backend falls back to Django's interpreted renderer.

The compiled function replaces ``Template._render`` — it receives a fully
bound ``Context`` (the caller reproduces ``Template.render``'s
``render_context.push_state`` / ``bind_template`` bookkeeping) and returns
the rendered string.

Exactness strategy, per the roadmap's optimization principle:

- Text and string literals are folded to constants at compile time.
- Variable lookups get an inline fast path that mirrors the success path of
  ``Variable._resolve_lookup`` (dict lookup first, guarded by the same
  ``hasattr(type(x), "__getitem__")`` check, bit-by-bit) plus the common-type
  fast path of ``render_value_in_context`` (exact ``str``/``SafeString``).
- The moment anything deviates from the provably-identical happy path — a
  lookup failure, a callable, an unusual value type, filters, translation —
  the generated code delegates to the *original node's* ``render()`` (or to
  Django's ``render_value_in_context``), so slow-path semantics are Django's
  own code, byte-identical by construction.

The fast-path exception tuple matches the one ``_resolve_lookup`` catches on
its dictionary-lookup attempt; anything outside it must propagate, exactly
as it would from Django's renderer. A deviation costs one re-resolution from
scratch through the original node (observable only as a repeated
``__getitem__`` on pathological objects).
"""

from __future__ import annotations

import logging

from django.template.base import TextNode, Variable, VariableNode, render_value_in_context
from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe

logger = logging.getLogger("dtc")

# Exactly the exceptions Variable._resolve_lookup catches on the dictionary
# lookup attempt. UnicodeDecodeError is a ValueError subclass, so it lands in
# the slow path, whose VariableNode.render handles it as Django does.
_LOOKUP_EXC = (TypeError, AttributeError, KeyError, ValueError, IndexError)


class _Slow:
    """Non-callable sentinel: 'bail out to the original node'."""

    __slots__ = ()


_SLOW = _Slow()


def compile_template(template):
    """Compile a ``django.template.base.Template`` to a render callable.

    Returns a callable ``(context) -> str`` where ``context`` is a fully
    bound ``django.template.Context``, or ``None`` if this template can't
    be compiled yet (the caller must fall back to ``template.render``).
    """
    if template.engine.debug:
        # Debug engines render through render_annotated() for exception
        # annotation and the debug error page; don't compete with that.
        return None
    try:
        return _compile(template)
    except Exception:
        # A compiler bug must never break rendering; fall back to Django.
        logger.exception("dtc failed to compile %r; falling back", template.name)
        return None


def _compile(template):
    namespace = {
        "_LOOKUP_EXC": _LOOKUP_EXC,
        "_SLOW": _SLOW,
        "_escape": escape,
        "_render_value": render_value_in_context,
        "_SafeString": SafeString,
        "_mark_safe": mark_safe,
    }
    lines = [
        "def _dtc_render(context):",
        "    _autoescape = context.autoescape",
        "    _context_get = context.__getitem__",
        "    _parts = []",
        "    _append = _parts.append",
    ]
    for i, node in enumerate(template.nodelist):
        node_type = type(node)  # exact type: subclasses may change semantics
        if node_type is TextNode:
            if node.s:
                lines.append(f"    _append({node.s!r})")
        elif node_type is VariableNode:
            _emit_variable(lines, namespace, node, i)
        else:
            return None  # phase 1: fall back on any tag
    lines.append("    return _mark_safe(''.join(_parts))")

    source = "\n".join(lines)
    code = compile(source, f"<dtc:{template.name or 'unnamed'}>", "exec")
    exec(code, namespace)
    render = namespace["_dtc_render"]
    render.__dtc_source__ = source
    return render


def _emit_variable(lines, namespace, node, i):
    """Emit code for one ``{{ ... }}`` node.

    Constant-fold what is provably constant, emit the inline fast path for
    plain lookups, and bridge everything else to the original node.
    """
    fe = node.filter_expression
    var = fe.var
    if not fe.filters:
        if not isinstance(var, Variable):
            # Parse-time constant, already resolved by FilterExpression
            # (quoted literals arrive as SafeString: never escaped, not
            # localized, so their rendering is context-independent).
            if type(var) in (str, SafeString):
                lines.append(f"    _append({str(var)!r})")
                return
            # None, or a lazy translation proxy: bridge.
        elif var.lookups is None and not var.translate:
            if isinstance(var.literal, str):
                # Quoted literal, mark_safe'd at parse time: fold.
                lines.append(f"    _append({str(var.literal)!r})")
                return
            # Numeric literal: rendering depends on runtime localization.
            namespace[f"_literal_{i}"] = var.literal
            lines.append(f"    _append(_render_value(_literal_{i}, context))")
            return
        elif not var.translate:
            _emit_fast_lookup(lines, namespace, node, var.lookups, i)
            return
    # Everything else (filters, translation, odd parses): the original node.
    namespace[f"_node_{i}"] = node
    lines.append(f"    _append(_node_{i}.render(context))")


# Generated-code templates for one lookup. These read exactly like the code
# they emit; _emit_block() adds the function-body indent. The stanzas stay at
# constant depth — a step that bails sets _value = _SLOW and the later stanzas
# skip themselves — so any number of lookup bits nests no deeper than this.

_LOOKUP_FIRST = """\
try:
    _value = _context_get({bit!r})
    if callable(_value):
        _value = _SLOW
"""

# Django's branch order, exactly: dictionary lookup if the type is
# subscriptable (Django's own guard), attribute lookup otherwise. A failure
# where Django would keep going (e.g. subscript miss -> getattr) lands in the
# except below and defers to the original node instead.
_LOOKUP_STEP = """\
    if _value is not _SLOW:
        _value = _value[{bit!r}] if hasattr(type(_value), '__getitem__') else getattr(_value, {bit!r})
        if callable(_value):
            _value = _SLOW
"""

# except clauses: the tuple Variable._resolve_lookup catches, then its
# catch-all (exceptions flagged silent_variable_failure render as
# string_if_invalid via the slow-path replay; everything else propagates,
# as Django re-raises). The tail inlines render_value_in_context for the two
# overwhelmingly common, provably identical types; everything else goes
# through the real thing.
_LOOKUP_FINISH = """\
except _LOOKUP_EXC:
    _value = _SLOW
except Exception as _exc:
    if getattr(_exc, 'silent_variable_failure', False):
        _value = _SLOW
    else:
        raise
if _value is _SLOW:
    _append(_node_{i}.render(context))
else:
    _value_type = _value.__class__
    if _value_type is str:
        _append(_escape(_value) if _autoescape else _value)
    elif _value_type is _SafeString:
        _append(_value)
    else:
        _append(_render_value(_value, context))
"""


def _emit_block(lines, block):
    lines.extend("    " + line for line in block.splitlines())


def _emit_fast_lookup(lines, namespace, node, lookups, i):
    """Inline the success path of ``Variable._resolve_lookup``.

    The first bit resolves against the Context (only dict-stack lookup can
    succeed there, so ``context[bit]`` is exact); later bits follow the
    templates above. Any lookup failure, and any callable result (Django may
    call it, substitute ``string_if_invalid``, or leave it), defers to the
    original node.
    """
    namespace[f"_node_{i}"] = node
    first, *rest = lookups
    _emit_block(lines, _LOOKUP_FIRST.format(bit=first))
    for bit in rest:
        _emit_block(lines, _LOOKUP_STEP.format(bit=bit))
    _emit_block(lines, _LOOKUP_FINISH.format(i=i))
