"""dtc -- a drop-in, compiling replacement for Django's template engine."""

__version__ = "0.0.1"

__all__ = ["ContextSafeViolation", "declare_safe", "declare_writes", "__version__"]


class ContextSafeViolation(Exception):
    """A node declared ``dtc_context_safe`` broke the declaration's contract.

    Raised only under ``DTC_CHECK_DECLARATIONS=1`` (see ``declare_safe``);
    without it a wrong declaration silently produces wrong output.
    """


def declare_safe(obj):
    """Declare a custom template tag context-safe, letting the compiler keep
    its read optimizations around it.

    ``obj`` is either a ``django.template.Node`` subclass (a raw
    ``register.tag`` tag) or the function registered with
    ``@simple_tag(takes_context=True)`` / ``@inclusion_tag(takes_context=True)``.
    Sets ``obj.dtc_context_safe = True`` and returns ``obj``, so it works as a
    decorator. Tags you own can skip this helper and set the class attribute
    directly — stock Django ignores it, so no dtc import is needed.

    The declaration is a promise about every ``render()`` call (for a
    function, every call):

    (a) The context stack and every mapping on it are left exactly as found:
        no ``push``/``pop``/``__setitem__``/``del`` visible after return
        (balanced internal push/pop is fine). Effects of rendering child
        nodelists are exempt — see (d).
    (b) No per-render state keyed by the node's identity — nothing like
        ``context.render_context[self]`` or mutable state on ``self``
        (Django's CycleNode/IfChangedNode pattern). Caches derived purely
        from the parsed arguments are fine.
    (c) Behavior depends only on the parsed source: node instances parsed
        from identical source are interchangeable (dtc may render other
        same-source template instances through one parse's node objects).
    (d) Any nodelist the node renders is listed in Django's
        ``child_nodelists`` attribute. The compiler analyzes those children
        itself, so their effects (including context writes by nested tags)
        are exempt from (a). A nodelist rendered but *not* listed hides
        nested writers from the compiler and produces wrong output — this
        is the one part of the contract nothing can check.

    Reading the context is always fine, as is setting
    ``context.autoescape`` (compiled code re-reads it after every bridged
    call). Clauses (b) and (c) do not apply to takes_context functions —
    only (a) does.

    Subclasses inherit the declaration along with the ``render()`` it
    covers; a subclass whose ``render()`` no longer qualifies must set
    ``dtc_context_safe = False``.

    Run your test suite with ``DTC_CHECK_DECLARATIONS=1`` to verify
    declarations: declared-safe renders are then checked against clauses
    (a) and (b) and raise ``ContextSafeViolation`` on violation.
    """
    if isinstance(obj, type):
        from django.template.base import Node

        if not issubclass(obj, Node):
            raise TypeError(
                f"declare_safe() expects a django.template.Node subclass or a "
                f"takes_context tag function, got the class {obj!r}"
            )
    elif not callable(obj):
        raise TypeError(
            f"declare_safe() expects a django.template.Node subclass or a "
            f"takes_context tag function, got {obj!r}"
        )
    obj.dtc_context_safe = True
    return obj


def declare_writes(node_class, *attr_names):
    """Declare that a Node subclass's ``render()`` writes exactly the
    context keys held in the named *instance attributes* — and follows the
    rest of the ``declare_safe`` contract for everything else.

    The attributes are named (rather than the keys themselves) because
    write targets are parse-time data::

        class StoreNode(Node):
            def __init__(self, nodelist, save_to):
                self.nodelist = nodelist
                self.save_to = save_to     # {% store as NAME %}
            def render(self, context):
                context[self.save_to] = self.nodelist.render(context)
                return ""

        dtc.declare_writes(StoreNode, "save_to")
        # or, on a class you own, equivalently:
        #     dtc_context_writes = ("save_to",)

    The promise, per ``render()`` call: the only context mutations are
    ``context[k] = value`` for keys held in the named attributes — no other
    writes, no deletions, no net push/pop, and clauses (b)–(d) of the
    ``declare_safe`` contract. An attribute holding None means an optional
    target unused at that site. With no attributes this is equivalent to
    ``declare_safe``. Only Node subclasses can declare writes (a
    ``takes_context`` function's targets aren't inspectable); subclasses
    inherit the declaration.

    Verify with ``DTC_CHECK_DECLARATIONS=1``: writes outside the declared
    keys raise ``ContextSafeViolation``.
    """
    from django.template.base import Node

    if not (isinstance(node_class, type) and issubclass(node_class, Node)):
        raise TypeError(
            f"declare_writes() expects a django.template.Node subclass, "
            f"got {node_class!r}"
        )
    if not all(isinstance(a, str) for a in attr_names):
        raise TypeError("declare_writes() attribute names must be strings")
    node_class.dtc_context_writes = attr_names
    return node_class
