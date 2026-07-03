"""Compile parsed Django templates to Python.

Phases 1–3: templates consisting of text, ``{{ variables }}`` (with
filters), ``{% if %}``, ``{% for %}``, ``{% with %}``,
``{% autoescape %}``, ``{% comment %}``, ``{% verbatim %}``, and
``{% load %}`` compile to a generated Python function; any other tag makes
``compile_template`` return ``None`` and the backend falls back to Django's
interpreted renderer.

The compiled function replaces ``Template._render`` — it receives a fully
bound ``Context`` (the caller reproduces ``Template.render``'s
``render_context.push_state`` / ``bind_template`` bookkeeping) and returns
the rendered string.

Scoping design (phase 3 decision): compiled code performs *real* context
operations — ``context.push()``/``pop()`` around loops and ``{% with %}``,
real ``context[var] = item`` writes for loop variables — exactly as
Django's nodes do. Locals-based scoping would be faster still, but it would
break every mechanism that resolves against the live context (slow-path
node replays, filter variable arguments, compiled templates rendered via
``{% include %}`` from interpreted ones); that optimization belongs with
the context-flattening work, designed once. What the analysis pass proves
unused is still skipped: the ``forloop`` dict is only built and updated
when some variable in the template starts with ``forloop``.

Exactness strategy, per the roadmap's optimization principle:

- Text, string literals, and ``{% verbatim %}`` fold to constants.
- Variable lookups get an inline fast path that mirrors the success path of
  ``Variable._resolve_lookup`` plus the common-type fast path of
  ``render_value_in_context`` (exact ``str``/``SafeString``).
- Filters call the registered filter function directly, with behavior-flag
  (``is_safe``/``needs_autoescape``/``expects_localtime``) specialization
  decided at compile time; custom filters compile natively.
- ``{% for %}`` compiles to a real Python loop, mirroring ``ForNode.render``
  line by line (including its quirks: the empty branch renders inside the
  pushed scope; the per-iteration unpack pop is not exception-protected).
  Iteration output appends into the top-level parts buffer directly instead
  of Django's per-loop list-and-join.
- ``{% if %}`` conditions evaluate through Django's own parsed condition
  objects (whose operator nodes swallow exceptions individually — that
  semantics is theirs to keep); the overwhelmingly common single-variable
  condition gets the inline lookup fast path first.
- The moment anything deviates from the provably-identical happy path, the
  generated code delegates to the original node/condition/FilterExpression,
  so slow-path semantics are Django's own code, byte-identical by
  construction. A deviation costs one re-resolution from scratch
  (observable only as a repeated ``__getitem__`` on pathological objects).
"""

from __future__ import annotations

import itertools
import logging

from django.template.base import (
    TextNode,
    Variable,
    VariableDoesNotExist,
    VariableNode,
    render_value_in_context,
)
from django.template.defaulttags import (
    AutoEscapeControlNode,
    CommentNode,
    ForNode,
    IfNode,
    LoadNode,
    TemplateLiteral,
    VerbatimNode,
    WithNode,
)
from django.template.loader_tags import BlockNode, ExtendsNode, IncludeNode
from django.utils.html import escape
from django.utils.safestring import SafeData, SafeString, mark_safe
from django.utils.timezone import template_localtime

from . import runtime

logger = logging.getLogger("dtc")

# Exactly the exceptions Variable._resolve_lookup catches on the dictionary
# lookup attempt. UnicodeDecodeError is a ValueError subclass, so it lands in
# the slow path, whose VariableNode.render handles it as Django does.
_LOOKUP_EXC = (TypeError, AttributeError, KeyError, ValueError, IndexError)


class _Slow:
    """Non-callable sentinel: 'bail out to the original node'."""

    __slots__ = ()


_SLOW = _Slow()


class _Uncompilable(Exception):
    """Raised by the analysis pass on any construct we can't compile yet."""


def _unpack_loop_item(loopvars, item):
    """Multi-variable loop unpacking, verbatim from ForNode.render."""
    num_loopvars = len(loopvars)
    try:
        len_item = len(item)
    except TypeError:  # not an iterable
        len_item = 1
    if num_loopvars != len_item:
        raise ValueError(
            "Need {} values to unpack in for loop; got {}. ".format(
                num_loopvars, len_item
            ),
        )
    return dict(zip(loopvars, item))


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
    except _Uncompilable:
        return None
    except Exception:
        # A compiler bug must never break rendering; fall back to Django.
        logger.exception("dtc failed to compile %r; falling back", template.name)
        return None


_PREAMBLE = (
    "    _autoescape = context.autoescape\n"
    "    _context_get = context.__getitem__\n"
    "    _context_set = context.__setitem__\n"
    "    _parts = []\n"
    "    _append = _parts.append\n"
)


def _compile(template):
    analysis = _Analysis()
    analysis.walk(template.nodelist)  # raises _Uncompilable

    codegen = _Codegen(
        forloop_needed="forloop" in analysis.first_bits or analysis.force_forloop
    )
    body = codegen.nodelist_block(template.nodelist)
    # Block bodies compile to standalone functions (they're invoked through
    # BlockContext, possibly by a different template in the chain).
    source = "".join(
        f"def {name}(context):\n{_PREAMBLE}{_indented(block_body)}"
        "    return _mark_safe(''.join(_parts))\n"
        for name, block_body in codegen.block_defs
    )
    source += (
        "def _dtc_render(context):\n"
        + _PREAMBLE
        + _indented(body)
        + "    return _mark_safe(''.join(_parts))\n"
    )
    code = compile(source, f"<dtc:{template.name or 'unnamed'}>", "exec")
    exec(code, codegen.namespace)
    for node, name in codegen.block_attach:
        node._dtc_body = codegen.namespace[name]
    render = codegen.namespace["_dtc_render"]
    render.__dtc_source__ = source
    return render


# --- analysis pass ------------------------------------------------------------
# One walk over the tree: validates that every node is a type we compile
# (anything else raises _Uncompilable) and collects the first bit of every
# Variable the template can ever resolve. For a template with no inheritance
# nodes that enumeration is complete, which is what makes "skip forloop
# maintenance when 'forloop' is never referenced" exact. A {% block %} or
# {% include %} *inside a loop* renders content this template can't see (a
# child's override, another template) which may reference forloop — those
# force maintenance on.


class _Analysis:
    def __init__(self):
        self.first_bits = set()
        self.force_forloop = False

    def walk(self, nodelist, in_loop=False):
        for node in nodelist:
            node_type = type(node)  # exact type: subclasses change semantics
            if node_type in (TextNode, CommentNode, VerbatimNode, LoadNode):
                pass
            elif node_type is VariableNode:
                self._fe(node.filter_expression)
            elif node_type is IfNode:
                for condition, branch in node.conditions_nodelists:
                    if condition is not None:
                        self._condition(condition)
                    self.walk(branch, in_loop)
            elif node_type is ForNode:
                self._fe(node.sequence)
                self.walk(node.nodelist_loop, True)
                # The empty branch runs outside the iteration.
                self.walk(node.nodelist_empty, in_loop)
            elif node_type is WithNode:
                for fe in node.extra_context.values():
                    self._fe(fe)
                self.walk(node.nodelist, in_loop)
            elif node_type is AutoEscapeControlNode:
                self.walk(node.nodelist, in_loop)
            elif node_type is BlockNode:
                if in_loop:
                    self.force_forloop = True
                # Body compilation is best-effort (an uncompilable body
                # renders interpreted through render_block).
                self._best_effort(node.nodelist, in_loop)
            elif node_type is IncludeNode:
                if in_loop:
                    self.force_forloop = True
            elif node_type is ExtendsNode:
                for block in node.blocks.values():
                    self._best_effort(block.nodelist, False)
            else:
                raise _Uncompilable(node_type.__name__)

    def _best_effort(self, nodelist, in_loop):
        try:
            self.walk(nodelist, in_loop)
        except _Uncompilable:
            pass

    def _fe(self, fe):
        if isinstance(fe.var, Variable) and fe.var.lookups:
            self.first_bits.add(fe.var.lookups[0])
        for _func, args in fe.filters:
            for is_lookup, arg in args:
                if is_lookup and isinstance(arg, Variable) and arg.lookups:
                    self.first_bits.add(arg.lookups[0])

    def _condition(self, condition):
        value = getattr(condition, "value", None)  # TemplateLiteral leaf
        if value is not None:
            self._fe(value)
        for attr in ("first", "second"):  # smartif operator children
            child = getattr(condition, attr, None)
            if child is not None:
                self._condition(child)


# --- generated-code templates -------------------------------------------------
# These read exactly like the code they emit; _indented() adds nesting.
# Lookup stanzas stay at constant depth — a step that bails sets
# _value = _SLOW and the later stanzas skip themselves — so any number of
# lookup bits nests no deeper than this.

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

# IfNode.render's condition evaluation, verbatim.
_CONDITION_EVAL = """\
try:
    _match = _cond_{i}.eval(context)
except _VariableDoesNotExist:
    _match = None
"""

# Fast tail for a bare single-variable condition ({% if x %}): the lookup
# stanzas ran first; any bail re-evaluates through Django's condition object
# (TemplateLiteral.eval resolves with ignore_failures=True, and IfNode maps
# VariableDoesNotExist to None).
_CONDITION_FAST_TAIL = """\
if _value is _SLOW:
    try:
        _match = _cond_{i}.eval(context)
    except _VariableDoesNotExist:
        _match = None
else:
    _match = _value
"""

# ForNode.render, reshaped: the pushed scope is a try/finally (Django's
# `with context.push()`), the empty branch renders inside it, iteration
# output appends straight into _parts.
_FOR_HEAD = """\
context.push()
try:
    _values_{i} = _seq_{i}.resolve(context, True)
    if _values_{i} is None:
        _values_{i} = []
    if not hasattr(_values_{i}, '__len__'):
        _values_{i} = list(_values_{i})
    _len_{i} = len(_values_{i})
    if _len_{i} < 1:
{empty}\
    else:
{loop}\
finally:
    context.pop()
"""

_FORLOOP_UPDATE = """\
_forloop_{i}['counter0'] = _index_{i}
_forloop_{i}['counter'] = _index_{i} + 1
_forloop_{i}['revcounter'] = _len_{i} - _index_{i}
_forloop_{i}['revcounter0'] = _len_{i} - _index_{i} - 1
_forloop_{i}['first'] = _index_{i} == 0
_forloop_{i}['last'] = _index_{i} == _len_{i} - 1
"""


def _indented(block, depth=1):
    pad = "    " * depth
    return "".join(pad + line + "\n" for line in block.splitlines())


class _Codegen:
    """Emit the render-function body, one block string per node."""

    def __init__(self, forloop_needed):
        self.forloop_needed = forloop_needed
        self.namespace = {
            "_LOOKUP_EXC": _LOOKUP_EXC,
            "_SLOW": _SLOW,
            "_escape": escape,
            "_render_value": render_value_in_context,
            "_SafeData": SafeData,
            "_SafeString": SafeString,
            "_mark_safe": mark_safe,
            "_template_localtime": template_localtime,
            "_VariableDoesNotExist": VariableDoesNotExist,
            "_unpack": _unpack_loop_item,
            "_render_block": runtime.render_block,
            "_render_extends": runtime.render_extends,
            "_render_include": runtime.render_include,
        }
        self._ids = itertools.count()
        self.block_defs = []  # (function name, body) per compiled block
        self.block_attach = []  # (BlockNode, function name) to bind after exec
        self._blocks_seen = set()

    def uid(self):
        return next(self._ids)

    def nodelist_block(self, nodelist):
        blocks = [self.visit(node) for node in nodelist]
        return "".join(blocks) or "pass\n"

    def visit(self, node):
        handler = self._handlers.get(type(node))
        if handler is None:  # reachable via best-effort block bodies
            raise _Uncompilable(type(node).__name__)
        return handler(self, node)

    # --- leaves ---------------------------------------------------------

    def visit_text(self, node):
        return f"_append({node.s!r})\n" if node.s else ""

    def visit_verbatim(self, node):
        return f"_append({node.content!r})\n" if node.content else ""

    def visit_silent(self, node):  # {% comment %}, {% load %}
        return ""

    # --- variables ------------------------------------------------------

    def visit_variable(self, node):
        """Constant-fold what is provably constant, emit the inline fast
        path for plain lookups and direct calls for filters, and bridge
        everything else to the original node."""
        i = self.uid()
        fe = node.filter_expression
        var = fe.var

        # The value ahead of any filters: a constant assignment, or the
        # inline lookup (`guarded`: _value may be _SLOW).
        guarded = False
        if isinstance(var, Variable) and not var.translate:
            if var.lookups is None:
                if not fe.filters:
                    if isinstance(var.literal, str):
                        # Quoted literal, mark_safe'd at parse time: fold.
                        return f"_append({str(var.literal)!r})\n"
                    # Numeric literal: rendering depends on localization.
                    self.namespace[f"_literal_{i}"] = var.literal
                    return f"_append(_render_value(_literal_{i}, context))\n"
                self.namespace[f"_literal_{i}"] = var.literal
                setup = f"_value = _literal_{i}\n"
            else:
                setup = self._lookup_stanzas(var.lookups)
                guarded = True
        elif not isinstance(var, Variable) and fe.filters:
            # Parse-time constant (SafeString, or a lazy i18n proxy that
            # must keep translating per render): pass to filters as-is.
            self.namespace[f"_literal_{i}"] = var
            setup = f"_value = _literal_{i}\n"
        elif not isinstance(var, Variable) and type(var) in (str, SafeString):
            # Constant with no filters: never escaped, not localized.
            return f"_append({str(var)!r})\n"
        else:
            # translate flag, or odd parses (e.g. constant that resolved
            # to None): the original node.
            self.namespace[f"_node_{i}"] = node
            return f"_append(_node_{i}.render(context))\n"

        if fe.filters:
            filters = "".join(
                self._filter_call(func, args, i, j)
                for j, (func, args) in enumerate(fe.filters)
            )
            body = _APPLY_FILTERS.format(
                filters=_indented(filters), output=_indented(_OUTPUT)
            )
        else:
            body = _OUTPUT

        if guarded:
            self.namespace[f"_node_{i}"] = node
            return setup + _SLOW_OR_ELSE.format(i=i, body=_indented(body))
        return setup + body

    def _lookup_stanzas(self, lookups):
        first, *rest = lookups
        block = _LOOKUP_FIRST.format(bit=first)
        for bit in rest:
            block += _LOOKUP_STEP.format(bit=bit)
        return block + _LOOKUP_EXCEPT

    def _filter_call(self, func, args, i, j):
        """One filter application, mirroring FilterExpression.resolve's
        loop. The behavior flags Django reads per render are constant per
        function, so the specialization happens here, at compile time."""
        filter_name = f"_filter_{i}_{j}"
        self.namespace[filter_name] = func
        is_safe = getattr(func, "is_safe", False)
        # is_safe needs the filter's *input* around after the call, to
        # decide whether the result inherits its safety.
        input_name = "_input" if is_safe else "_value"
        call_args = [input_name]
        for k, (is_lookup, arg) in enumerate(args):
            arg_name = f"_arg_{i}_{j}_{k}"
            if not is_lookup:
                if isinstance(arg, str):
                    # mark_safe of a plain/Safe str is deterministic: fold.
                    self.namespace[arg_name] = mark_safe(arg)
                    call_args.append(arg_name)
                else:
                    # Lazy i18n constant: translation happens per render.
                    self.namespace[arg_name] = arg
                    call_args.append(f"_mark_safe({arg_name})")
            elif arg.lookups is None and not arg.translate:
                self.namespace[arg_name] = arg.literal
                call_args.append(arg_name)
            else:
                # Django's own Variable.resolve, failures and all (a missing
                # filter argument raises VariableDoesNotExist, unlike a
                # missing variable ahead of the filters).
                self.namespace[arg_name] = arg
                call_args.append(f"{arg_name}.resolve(context)")
        if getattr(func, "needs_autoescape", False):
            call_args.append("autoescape=_autoescape")
        call = f"{filter_name}({', '.join(call_args)})"

        block = ""
        if getattr(func, "expects_localtime", False):
            block += "_value = _template_localtime(_value, context.use_tz)\n"
        if is_safe:
            block += (
                "_input = _value\n"
                f"_value = {call}\n"
                "if isinstance(_input, _SafeData):\n"
                "    _value = _mark_safe(_value)\n"
            )
        else:
            block += f"_value = {call}\n"
        return block

    # --- control flow ----------------------------------------------------

    def visit_if(self, node):
        return self._if_branches(node.conditions_nodelists)

    def _if_branches(self, pairs):
        (condition, nodelist), *rest = pairs
        if condition is None:  # {% else %}: always the last clause
            return self.nodelist_block(nodelist)
        i = self.uid()
        self.namespace[f"_cond_{i}"] = condition
        block = self._condition_eval(condition, i)
        block += "if _match:\n" + _indented(self.nodelist_block(nodelist))
        if rest:
            block += "else:\n" + _indented(self._if_branches(rest))
        return block

    def _condition_eval(self, condition, i):
        # Fast path for the most common condition by far: a bare variable
        # ({% if user %}). Anything else — operators, filters — evaluates
        # through Django's own parsed condition object, which owns the
        # smart-if semantics (operator nodes swallow exceptions to False).
        if type(condition) is TemplateLiteral:
            fe = condition.value
            var = fe.var
            if (
                not fe.filters
                and isinstance(var, Variable)
                and var.lookups is not None
                and not var.translate
            ):
                return self._lookup_stanzas(var.lookups) + _CONDITION_FAST_TAIL.format(
                    i=i
                )
        return _CONDITION_EVAL.format(i=i)

    def visit_with(self, node):
        i = self.uid()
        items = []
        for k, (key, fe) in enumerate(node.extra_context.items()):
            name = f"_with_{i}_{k}"
            self.namespace[name] = fe
            items.append(f"{key!r}: {name}.resolve(context)")
        # Values resolve before the push, as in WithNode.render.
        block = "context.push({" + ", ".join(items) + "})\n"
        block += "try:\n" + _indented(self.nodelist_block(node.nodelist))
        block += "finally:\n    context.pop()\n"
        return block

    def visit_autoescape(self, node):
        # AutoEscapeControlNode sets and restores with no exception guard;
        # match it. The hoisted _autoescape local tracks context.autoescape
        # so filter/output emission inside the block sees the new setting.
        i = self.uid()
        setting = bool(node.setting)
        return (
            f"_saved_autoescape_{i} = _autoescape\n"
            f"_autoescape = context.autoescape = {setting!r}\n"
            + self.nodelist_block(node.nodelist)
            + f"_autoescape = context.autoescape = _saved_autoescape_{i}\n"
        )

    def visit_for(self, node):
        i = self.uid()
        self.namespace[f"_seq_{i}"] = node.sequence

        body = ""
        if self.forloop_needed:
            body += _FORLOOP_UPDATE.format(i=i)
        if len(node.loopvars) == 1:
            body += f"_context_set({node.loopvars[0]!r}, _item_{i})\n"
            body += self.nodelist_block(node.nodelist_loop)
        else:
            # Unpack pushes a scope per iteration; the pop is deliberately
            # not exception-protected, matching ForNode.render.
            body += f"context.update(_unpack({tuple(node.loopvars)!r}, _item_{i}))\n"
            body += self.nodelist_block(node.nodelist_loop)
            body += "context.pop()\n"

        loop = ""
        if node.is_reversed:
            loop += f"_values_{i} = reversed(_values_{i})\n"
        if self.forloop_needed:
            loop += (
                f"_parentloop_{i} = context['forloop'] if 'forloop' in context else {{}}\n"
                f"_forloop_{i} = context['forloop'] = {{'parentloop': _parentloop_{i}}}\n"
                f"for _index_{i}, _item_{i} in enumerate(_values_{i}):\n"
            )
        else:
            loop += f"for _item_{i} in _values_{i}:\n"
        loop += _indented(body)

        return _FOR_HEAD.format(
            i=i,
            empty=_indented(self.nodelist_block(node.nodelist_empty), 2),
            loop=_indented(loop, 2),
        )

    # --- inheritance and inclusion ----------------------------------------

    def visit_block(self, node):
        i = self.uid()
        self.namespace[f"_node_{i}"] = node
        self._compile_block_body(node)
        return f"_append(_render_block(_node_{i}, context))\n"

    def visit_extends(self, node):
        # The extends machinery (parent resolution, BlockContext population)
        # runs through the runtime mirror; this template's blocks compile to
        # functions the parent's block sites will pick up from BlockContext.
        i = self.uid()
        self.namespace[f"_node_{i}"] = node
        for block in node.blocks.values():
            self._compile_block_body(block)
        return f"_append(_render_extends(_node_{i}, context))\n"

    def visit_include(self, node):
        i = self.uid()
        self.namespace[f"_node_{i}"] = node
        return f"_append(_render_include(_node_{i}, context))\n"

    def _compile_block_body(self, node):
        """Best-effort: a block whose body we can't compile still works —
        render_block falls back to its nodelist. node.blocks of an
        ExtendsNode includes nested blocks, and visiting an outer body
        compiles inner ones, hence the seen-guard."""
        if id(node) in self._blocks_seen:
            return
        self._blocks_seen.add(id(node))
        try:
            body = self.nodelist_block(node.nodelist)
        except _Uncompilable:
            return
        name = f"_dtc_block_{self.uid()}"
        self.block_defs.append((name, body))
        self.block_attach.append((node, name))

    _handlers = {
        TextNode: visit_text,
        VerbatimNode: visit_verbatim,
        CommentNode: visit_silent,
        LoadNode: visit_silent,
        VariableNode: visit_variable,
        IfNode: visit_if,
        WithNode: visit_with,
        AutoEscapeControlNode: visit_autoescape,
        ForNode: visit_for,
        BlockNode: visit_block,
        ExtendsNode: visit_extends,
        IncludeNode: visit_include,
    }
