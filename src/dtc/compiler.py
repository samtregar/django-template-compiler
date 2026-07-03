"""Compile parsed Django templates to Python.

Phases 1–2: templates consisting of ``TextNode``, ``VariableNode`` (with or
without filters), and ``{% load %}`` compile to a generated Python function;
anything else returns ``None`` and the backend falls back to Django's
interpreted renderer.

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
- Filters call the *registered filter function* directly — never a
  reimplementation. ``FilterExpression.resolve`` reads each function's
  behavior flags (``is_safe``/``needs_autoescape``/``expects_localtime``)
  via getattr on every render; those are constant per function, so codegen
  specializes on them at compile time. This means custom filters compile
  natively. String constant arguments fold (``mark_safe`` of a plain str is
  deterministic); lazy i18n constants and variable arguments resolve per
  render through Django's own objects.
- The moment anything deviates from the provably-identical happy path — a
  lookup failure, a callable, an unusual value type, translation — the
  generated code delegates to the *original node's* ``render()`` (or to
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
from django.template.defaulttags import LoadNode
from django.utils.html import escape
from django.utils.safestring import SafeData, SafeString, mark_safe
from django.utils.timezone import template_localtime

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
        "_SafeData": SafeData,
        "_SafeString": SafeString,
        "_mark_safe": mark_safe,
        "_template_localtime": template_localtime,
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
        elif node_type is LoadNode:
            pass  # {% load %} affects parsing only; renders as ""
        else:
            return None  # phase 3+: fall back on any other tag
    lines.append("    return _mark_safe(''.join(_parts))")

    source = "\n".join(lines)
    code = compile(source, f"<dtc:{template.name or 'unnamed'}>", "exec")
    exec(code, namespace)
    render = namespace["_dtc_render"]
    render.__dtc_source__ = source
    return render


# --- generated-code templates -------------------------------------------------
# These read exactly like the code they emit; _emit_block() adds the
# function-body indent, _indented() nests sub-blocks. Lookup stanzas stay at
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

# The tuple Variable._resolve_lookup catches, then its catch-all (exceptions
# flagged silent_variable_failure render as string_if_invalid via the
# slow-path replay; everything else propagates, as Django re-raises).
_LOOKUP_EXCEPT = """\
except _LOOKUP_EXC:
    _value = _SLOW
except Exception as _exc:
    if getattr(_exc, 'silent_variable_failure', False):
        _value = _SLOW
    else:
        raise
"""

# Inlines render_value_in_context for the two overwhelmingly common, provably
# identical types; everything else goes through the real thing.
_OUTPUT = """\
_value_type = _value.__class__
if _value_type is str:
    _append(_escape(_value) if _autoescape else _value)
elif _value_type is _SafeString:
    _append(_value)
else:
    _append(_render_value(_value, context))
"""

# VariableNode.render turns a UnicodeDecodeError from FilterExpression.resolve
# (which the filter chain is part of) into empty output.
_APPLY_FILTERS = """\
try:
{filters}\
except UnicodeDecodeError:
    pass
else:
{output}\
"""

_SLOW_OR_ELSE = """\
if _value is _SLOW:
    _append(_node_{i}.render(context))
else:
{body}\
"""


def _indented(block, depth=1):
    pad = "    " * depth
    return "".join(pad + line + "\n" for line in block.splitlines())


def _emit_block(lines, block):
    lines.extend("    " + line for line in block.splitlines())


def _emit_variable(lines, namespace, node, i):
    """Emit code for one ``{{ ... }}`` node.

    Constant-fold what is provably constant, emit the inline fast path for
    plain lookups and direct calls for filters, and bridge everything else
    to the original node.
    """
    fe = node.filter_expression
    var = fe.var

    # The value ahead of any filters: a constant assignment, or the inline
    # lookup (`guarded`: _value may be _SLOW and need the original node).
    guarded = False
    if isinstance(var, Variable) and not var.translate:
        if var.lookups is None:
            if not fe.filters:
                if isinstance(var.literal, str):
                    # Quoted literal, mark_safe'd at parse time: fold.
                    lines.append(f"    _append({str(var.literal)!r})")
                else:
                    # Numeric literal: rendering depends on localization.
                    namespace[f"_literal_{i}"] = var.literal
                    lines.append(f"    _append(_render_value(_literal_{i}, context))")
                return
            namespace[f"_literal_{i}"] = var.literal
            setup = f"_value = _literal_{i}\n"
        else:
            first, *rest = var.lookups
            setup = _LOOKUP_FIRST.format(bit=first)
            for bit in rest:
                setup += _LOOKUP_STEP.format(bit=bit)
            setup += _LOOKUP_EXCEPT
            guarded = True
    elif not isinstance(var, Variable) and fe.filters:
        # Parse-time constant (SafeString, or a lazy i18n proxy that must
        # keep translating per render): pass to the filters as-is.
        namespace[f"_literal_{i}"] = var
        setup = f"_value = _literal_{i}\n"
    elif not isinstance(var, Variable) and type(var) in (str, SafeString):
        # Constant with no filters: never escaped, not localized -> fold.
        lines.append(f"    _append({str(var)!r})")
        return
    else:
        # translate flag, or odd parses (e.g. constant that resolved to
        # None): the original node.
        namespace[f"_node_{i}"] = node
        lines.append(f"    _append(_node_{i}.render(context))")
        return

    if fe.filters:
        filters = "".join(
            _filter_call(namespace, func, args, i, j)
            for j, (func, args) in enumerate(fe.filters)
        )
        body = _APPLY_FILTERS.format(
            filters=_indented(filters), output=_indented(_OUTPUT)
        )
    else:
        body = _OUTPUT

    if guarded:
        namespace[f"_node_{i}"] = node
        block = setup + _SLOW_OR_ELSE.format(i=i, body=_indented(body))
    else:
        block = setup + body
    _emit_block(lines, block)


def _filter_call(namespace, func, args, i, j):
    """One filter application, mirroring FilterExpression.resolve's loop.

    The behavior flags Django reads per render are constant per function,
    so the specialization happens here, at compile time.
    """
    filter_name = f"_filter_{i}_{j}"
    namespace[filter_name] = func
    is_safe = getattr(func, "is_safe", False)
    # is_safe needs the filter's *input* around after the call, to decide
    # whether the result inherits its safety.
    input_name = "_input" if is_safe else "_value"
    call_args = [input_name]
    for k, (is_lookup, arg) in enumerate(args):
        arg_name = f"_arg_{i}_{j}_{k}"
        if not is_lookup:
            if isinstance(arg, str):
                # mark_safe of a plain/Safe str is deterministic: fold.
                namespace[arg_name] = mark_safe(arg)
                call_args.append(arg_name)
            else:
                # Lazy i18n constant: translation happens per render.
                namespace[arg_name] = arg
                call_args.append(f"_mark_safe({arg_name})")
        elif arg.lookups is None and not arg.translate:
            namespace[arg_name] = arg.literal
            call_args.append(arg_name)
        else:
            # Django's own Variable.resolve, failures and all (a missing
            # filter argument raises VariableDoesNotExist, unlike a missing
            # variable ahead of the filters).
            namespace[arg_name] = arg
            call_args.append(f"{arg_name}.resolve(context)")
    if getattr(func, "needs_autoescape", False):
        call_args.append("autoescape=_autoescape")
    call = f"{filter_name}({', '.join(call_args)})"

    block = ""
    if getattr(func, "expects_localtime", False):
        block += "_value = _template_localtime(_value, context.use_tz)\n"
    if is_safe:
        block += (
            f"_input = _value\n"
            f"_value = {call}\n"
            "if isinstance(_input, _SafeData):\n"
            "    _value = _mark_safe(_value)\n"
        )
    else:
        block += f"_value = {call}\n"
    return block
