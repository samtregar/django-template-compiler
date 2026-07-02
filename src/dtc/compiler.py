"""Compile parsed Django templates to Python.

This is the heart of dtc. A template is parsed with Django's own lexer and
parser (so syntax handling is Django's, byte for byte), and the resulting
node tree is translated to a Python function that renders the template.

Templates (or subtrees) that use constructs we can't compile yet fall back
to Django's interpreted render path, so behavior is always correct even
while coverage of the compiler grows.
"""

from __future__ import annotations


def compile_template(template):
    """Compile a ``django.template.base.Template`` to a render callable.

    Returns a callable ``(context) -> str`` where ``context`` is a fully
    bound ``django.template.Context``, or ``None`` if this template can't
    be compiled yet (the caller must fall back to ``template.render``).
    """
    # Stub: nothing is compilable yet; every template takes the fallback
    # path through Django's own renderer.
    return None
