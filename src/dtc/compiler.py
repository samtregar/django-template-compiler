"""Compile parsed Django templates to Python.

Every parseable template compiles (phases 1–5): text, ``{{ variables }}``
(with filters), ``{% if %}``, ``{% for %}``, ``{% with %}``,
``{% autoescape %}``, ``{% comment %}``, ``{% verbatim %}``,
``{% load %}``, inheritance (``{% block %}``/``{% extends %}``/
``{% include %}``), and ``@simple_tag``/``@inclusion_tag`` nodes get
dedicated code generation; any other node — third-party tags included —
bridges as ``node.render_annotated(context)`` against the live context,
which is exact because compiled code performs real context operations.
``compile_template`` returns ``None`` only for debug engines and on
internal compiler errors (fail-open).

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
import os

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
    FilterNode,
    ForNode,
    IfChangedNode,
    IfNode,
    LoadNode,
    SpacelessNode,
    TemplateLiteral,
    VerbatimNode,
    WithNode,
)
from django.templatetags.i18n import LanguageNode
from django.templatetags.l10n import LocalizeNode
from django.templatetags.tz import LocalTimeNode, TimezoneNode
from django.template.base import Template as BaseTemplate
from django.template.library import InclusionNode, SimpleNode
from django.template.loader_tags import BlockNode, ExtendsNode, IncludeNode
from django.utils import timezone as dj_timezone
from django.utils import translation
from django.utils.html import conditional_escape, escape, strip_spaces_between_tags
from django.utils.safestring import SafeData, SafeString, mark_safe
from django.utils.timezone import template_localtime

from . import runtime

logger = logging.getLogger("dtc")

#: When true (or DTC_STRICT=1 in the environment), internal compiler errors
#: raise instead of falling back to Django's renderer. Rendering stays
#: fail-open in production; the oracle suites run strict so a compiler bug
#: fails CI loudly rather than hiding behind the fallback.
STRICT = False

# Exactly the exceptions Variable._resolve_lookup catches on the dictionary
# lookup attempt. UnicodeDecodeError is a ValueError subclass, so it lands in
# the slow path, whose VariableNode.render handles it as Django does.
_LOOKUP_EXC = (TypeError, AttributeError, KeyError, ValueError, IndexError)


class _Slow:
    """Non-callable sentinel: 'bail out to the original node'."""

    __slots__ = ()


_SLOW = _Slow()


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


def _digit_lookup(current, bit, index):
    """One dotted-path step for an all-digits bit ({{ p.0 }}), in
    Variable._resolve_lookup's order: string subscript (dicts with digit
    keys), attribute, integer subscript (sequences). Any failure raises to
    the caller, which replays through the original node — with one
    documented shortcut: Django consults dir(current) before its integer
    lookup to re-raise exceptions from properties, which cannot apply to a
    name like '0' (dir entries are identifiers), so we skip that dir() call
    and its per-miss cost."""
    if hasattr(type(current), "__getitem__"):
        try:
            return current[bit]
        except _LOOKUP_EXC:
            try:
                return getattr(current, bit)
            except (TypeError, AttributeError):
                return current[index]
    return getattr(current, bit)


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
        if STRICT or os.environ.get("DTC_STRICT") == "1":
            raise
        from . import runtime

        runtime.stats["templates_error"] += 1
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
    render.__dtc_shareable__ = not codegen.uses_bridges
    return render


# --- analysis pass ------------------------------------------------------------
# One walk over the tree, collecting the first bit of every Variable the
# template can ever resolve. For known node types that enumeration is
# complete, which is what makes "skip forloop maintenance when 'forloop' is
# never referenced" exact. Constructs that render content this template
# can't see — a {% block %} override, an {% include %}d template, a bridged
# unknown tag, a takes_context simple_tag — force maintenance on when they
# sit inside a loop: they may read forloop, and some (IfChangedNode) even
# use the forloop dict itself as their loop-scope state marker.


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
                self.walk(node.nodelist, in_loop)
            elif node_type is IncludeNode:
                if in_loop:
                    self.force_forloop = True
            elif node_type is SpacelessNode:
                self.walk(node.nodelist, in_loop)
            elif node_type is FilterNode:
                self._fe(node.filter_expr)
                self.walk(node.nodelist, in_loop)
            elif node_type is IfChangedNode:
                # Its state frame IS the forloop dict when inside a loop.
                if in_loop:
                    self.force_forloop = True
                for fe in node._varlist:
                    self._fe(fe)
                self.walk(node.nodelist_true, in_loop)
                self.walk(node.nodelist_false, in_loop)
            elif node_type in (LocalizeNode, LocalTimeNode):
                self.walk(node.nodelist, in_loop)
            elif node_type is TimezoneNode:
                self._fe(node.tz)
                self.walk(node.nodelist, in_loop)
            elif node_type is LanguageNode:
                self._fe(node.language)
                self.walk(node.nodelist, in_loop)
            elif node_type in (SimpleNode, InclusionNode):
                # Arguments resolve in the outer context; a takes_context
                # function can read anything. (InclusionNode renders its
                # template with context.new() — isolated — so the template
                # itself can't see forloop.)
                for fe in list(node.args) + list(node.kwargs.values()):
                    self._fe(fe)
                if node_type is SimpleNode and node.takes_context and in_loop:
                    self.force_forloop = True
            else:
                # Unknown node: bridged at codegen. Its render can read
                # anything from the live context.
                if in_loop:
                    self.force_forloop = True

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

# All-digits bits ({{ p.0 }}) take the three-way branch via _digit_lookup;
# without this, sequence indexing would fail the fast path and replay the
# whole node on every access.
_LOOKUP_STEP_DIGIT = """\
    if _value is not _SLOW:
        _value = _digit_lookup(_value, {bit!r}, {index})
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
            "_digit_lookup": _digit_lookup,
            "_render_block": runtime.render_block,
            "_render_extends": runtime.render_extends,
            "_render_include": runtime.render_include,
            "_template_render": runtime.template_render,
            "_conditional_escape": conditional_escape,
            "_strip_spaces": strip_spaces_between_tags,
            "_tz_override": dj_timezone.override,
            "_lang_override": translation.override,
        }
        self._ids = itertools.count()
        self.block_defs = []  # (function name, body) per compiled block
        self.block_attach = []  # (BlockNode, function name) to bind after exec
        self._blocks_seen = set()
        self.uses_bridges = False

    def uid(self):
        return next(self._ids)

    def nodelist_block(self, nodelist):
        blocks = [self.visit(node) for node in nodelist]
        return "".join(blocks) or "pass\n"

    def visit(self, node):
        handler = self._handlers.get(type(node))
        if handler is None:
            return self.visit_bridge(node)
        return handler(self, node)

    def visit_bridge(self, node):
        """Any node type we don't compile runs as itself against the live
        context — exact because compiled code maintains a real Context and
        render_context. render_annotated (not render) honors third-party
        overrides; in non-debug engines the default implementation is a
        plain passthrough to render, matching NodeList.render."""
        i = self.uid()
        self.namespace[f"_node_{i}"] = node
        # Bridged nodes may keep per-node state keyed by their own identity
        # (IfChangedNode, CycleNode, ...). Embedding one makes this compiled
        # function specific to this parse: it must not be shared across
        # same-source template instances (see runtime.compiled_for).
        self.uses_bridges = True
        return f"_append(_node_{i}.render_annotated(context))\n"

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
            if bit.isdigit():
                block += _LOOKUP_STEP_DIGIT.format(bit=bit, index=int(bit))
            else:
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

    # --- container tags (bodies compile; the wrapper is mirrored) ----------
    # Bridging a container tag would force its whole subtree to render
    # interpreted, so containers get dedicated codegen. Leaf tags (url,
    # csrf_token, static, now, ...) stay bridged: their render *is* the
    # work, and a bridge costs the same as Django's own dispatch.

    def _subrender(self, nodelist, i):
        """Render a nodelist into _sub_{i} instead of the output buffer —
        the compiled equivalent of `output = self.nodelist.render(context)`
        (a SafeString, as NodeList.render returns)."""
        return (
            f"_saved_parts_{i} = _parts\n"
            "_parts = []\n"
            "_append = _parts.append\n"
            + self.nodelist_block(nodelist)
            + f"_sub_{i} = _mark_safe(''.join(_parts))\n"
            f"_parts = _saved_parts_{i}\n"
            "_append = _parts.append\n"
        )

    def visit_spaceless(self, node):
        i = self.uid()
        return (
            self._subrender(node.nodelist, i)
            + f"_append(_strip_spaces(_sub_{i}.strip()))\n"
        )

    def visit_filter_tag(self, node):
        # FilterNode.render: body renders first, then resolves the parsed
        # 'var|filters' expression with the output pushed as 'var'.
        i = self.uid()
        self.namespace[f"_ffe_{i}"] = node.filter_expr
        return (
            self._subrender(node.nodelist, i)
            + f"context.push({{'var': _sub_{i}}})\n"
            "try:\n"
            f"    _append(_ffe_{i}.resolve(context))\n"
            "finally:\n"
            "    context.pop()\n"
        )

    def visit_ifchanged(self, node):
        """IfChangedNode.render: state lives on the forloop dict inside
        loops (analysis forces the dict on), else on render_context, keyed
        by the node — which also makes this template non-shareable."""
        i = self.uid()
        self.namespace[f"_node_{i}"] = node
        self.uses_bridges = True  # identity-keyed state: per-parse function
        block = (
            f"_ifch_frame_{i} = context['forloop'] if 'forloop' in context"
            " else context.render_context\n"
            f"_ifch_frame_{i}.setdefault(_node_{i})\n"
        )
        if node._varlist:
            for k, fe in enumerate(node._varlist):
                self.namespace[f"_ifv_{i}_{k}"] = fe
            compare = ", ".join(
                f"_ifv_{i}_{k}.resolve(context, True)"
                for k in range(len(node._varlist))
            )
            block += (
                f"_ifch_cmp_{i} = [{compare}]\n"
                f"if _ifch_cmp_{i} != _ifch_frame_{i}[_node_{i}]:\n"
                f"    _ifch_frame_{i}[_node_{i}] = _ifch_cmp_{i}\n"
                + _indented(self.nodelist_block(node.nodelist_true))
            )
        else:
            # Without variables, the comparison value is the rendered body.
            block += (
                self._subrender(node.nodelist_true, i)
                + f"if _sub_{i} != _ifch_frame_{i}[_node_{i}]:\n"
                f"    _ifch_frame_{i}[_node_{i}] = _sub_{i}\n"
                f"    _append(_sub_{i})\n"
            )
        if node.nodelist_false:
            block += "else:\n" + _indented(self.nodelist_block(node.nodelist_false))
        return block

    def _flag_wrapper(self, node, attr, value):
        # LocalizeNode / LocalTimeNode: set a context flag, render, restore
        # (no exception guard, matching Django).
        i = self.uid()
        return (
            f"_saved_flag_{i} = context.{attr}\n"
            f"context.{attr} = {value!r}\n"
            + self.nodelist_block(node.nodelist)
            + f"context.{attr} = _saved_flag_{i}\n"
        )

    def visit_localize(self, node):
        return self._flag_wrapper(node, "use_l10n", node.use_l10n)

    def visit_localtime(self, node):
        return self._flag_wrapper(node, "use_tz", node.use_tz)

    def _override_wrapper(self, node, fe, manager):
        # TimezoneNode / LanguageNode: body renders under a with-block.
        i = self.uid()
        self.namespace[f"_ctxfe_{i}"] = fe
        return f"with {manager}(_ctxfe_{i}.resolve(context)):\n" + _indented(
            self.nodelist_block(node.nodelist)
        )

    def visit_timezone(self, node):
        return self._override_wrapper(node, node.tz, "_tz_override")

    def visit_language(self, node):
        return self._override_wrapper(node, node.language, "_lang_override")

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
        """node.blocks of an ExtendsNode includes nested blocks, and
        visiting an outer body compiles inner ones, hence the seen-guard."""
        if id(node) in self._blocks_seen:
            return
        self._blocks_seen.add(id(node))
        body = self.nodelist_block(node.nodelist)
        name = f"_dtc_block_{self.uid()}"
        self.block_defs.append((name, body))
        self.block_attach.append((node, name))

    # --- custom tag helpers (simple_tag / inclusion_tag) -------------------

    def _tag_call(self, node, i):
        """Build the generated call to a TagHelperNode's function, mirroring
        get_resolved_arguments: constant arguments fold, the rest resolve
        through Django's own FilterExpressions. kwargs go through a dict
        splat — parse_bits allows any \\w+ name for **kwargs functions,
        including Python keywords."""
        func_name = f"_tag_{i}"
        self.namespace[func_name] = node.func
        call_args = ["context"] if node.takes_context else []
        for k, fe in enumerate(node.args):
            call_args.append(self._arg_expr(fe, f"_targ_{i}_{k}"))
        if node.kwargs:
            items = ", ".join(
                f"{key!r}: {self._arg_expr(fe, f'_tkwarg_{i}_{k}')}"
                for k, (key, fe) in enumerate(node.kwargs.items())
            )
            call_args.append(f"**{{{items}}}")
        return f"{func_name}({', '.join(call_args)})"

    def _arg_expr(self, fe, name):
        """An expression for one FilterExpression argument: folded when its
        resolution is provably context-independent."""
        var = fe.var
        if not fe.filters:
            if not isinstance(var, Variable):
                if isinstance(var, str):  # parse-time constant
                    self.namespace[name] = var
                    return name
            elif var.lookups is None and not var.translate:
                self.namespace[name] = var.literal
                return name
        self.namespace[name] = fe
        return f"{name}.resolve(context)"

    def visit_simple(self, node):
        """SimpleNode.render, specialized at compile time (verified
        identical across Django 4.2–5.2): argument resolution inlined,
        target_var/autoescape branches decided now."""
        i = self.uid()
        call = self._tag_call(node, i)
        if node.target_var is not None:
            return f"context[{node.target_var!r}] = {call}\n"
        return (
            f"_value = {call}\n"
            "if _autoescape:\n"
            "    _value = _conditional_escape(_value)\n"
            "_append(_value)\n"
        )

    def visit_inclusion(self, node):
        """InclusionNode.render with the template-render call made
        compiled-aware. Only emitted when the filename form is known at
        compile time (str, Template, or a backend template) — the exotic
        iterable-of-names form bridges, which also sidesteps the one line
        of this method that differs between Django 4.2 and 5.2."""
        i = self.uid()
        filename = node.filename
        if isinstance(filename, str):
            resolve = f"    _incl_t = context.template.engine.get_template({filename!r})\n"
        elif isinstance(filename, BaseTemplate):
            self.namespace[f"_incl_file_{i}"] = filename
            resolve = f"    _incl_t = _incl_file_{i}\n"
        elif isinstance(getattr(filename, "template", None), BaseTemplate):
            self.namespace[f"_incl_file_{i}"] = filename.template
            resolve = f"    _incl_t = _incl_file_{i}\n"
        else:
            return self.visit_bridge(node)
        self.namespace[f"_node_{i}"] = node
        return (
            f"_incl_dict = {self._tag_call(node, i)}\n"
            f"_incl_t = context.render_context.get(_node_{i})\n"
            "if _incl_t is None:\n"
            + resolve
            + f"    context.render_context[_node_{i}] = _incl_t\n"
            "_incl_ctx = context.new(_incl_dict)\n"
            "_csrf = context.get('csrf_token')\n"
            "if _csrf is not None:\n"
            "    _incl_ctx['csrf_token'] = _csrf\n"
            "_append(_template_render(_incl_t, _incl_ctx))\n"
        )

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
        SimpleNode: visit_simple,
        InclusionNode: visit_inclusion,
        SpacelessNode: visit_spaceless,
        FilterNode: visit_filter_tag,
        IfChangedNode: visit_ifchanged,
        LocalizeNode: visit_localize,
        LocalTimeNode: visit_localtime,
        TimezoneNode: visit_timezone,
        LanguageNode: visit_language,
    }
