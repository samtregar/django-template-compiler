"""Django BACKENDS engine for dtc.

Usage in settings.py -- change only the BACKEND line of an existing
DjangoTemplates configuration:

    TEMPLATES = [
        {
            "BACKEND": "dtc.backend.DTCTemplates",
            "DIRS": [...],
            "APP_DIRS": True,
            "OPTIONS": {...},
        },
    ]

All OPTIONS accepted by django.template.backends.django.DjangoTemplates
(context_processors, libraries, builtins, autoescape, debug, loaders,
string_if_invalid, ...) are accepted here with identical meaning.
"""

from __future__ import annotations

from django.template import TemplateDoesNotExist
from django.template.backends.django import (
    DjangoTemplates,
    Template as DjangoTemplateProxy,
    reraise,
)
from django.template.context import make_context

from .compiler import compile_template


class DTCTemplates(DjangoTemplates):
    """Drop-in replacement for the DjangoTemplates backend.

    Parsing, template loading, and configuration are inherited unchanged
    from Django; only the render path differs. Templates that the compiler
    can handle render through generated Python code, everything else falls
    back to Django's interpreted renderer.
    """

    def from_string(self, template_code):
        return Template(self.engine.from_string(template_code), self)

    def get_template(self, template_name):
        try:
            return Template(self.engine.get_template(template_name), self)
        except TemplateDoesNotExist as exc:
            reraise(exc, self)


class Template(DjangoTemplateProxy):
    """Backend template proxy that prefers the compiled render path."""

    def __init__(self, template, backend):
        super().__init__(template, backend)
        self._compiled = compile_template(template)

    def render(self, context=None, request=None):
        compiled = self._compiled
        if compiled is None:
            return super().render(context=context, request=request)
        context = make_context(
            context, request, autoescape=self.backend.engine.autoescape
        )
        # Reproduce django.template.base.Template.render exactly: the
        # compiled function replaces _render, but the render_context state
        # push and template binding (which runs context processors on
        # RequestContext and exposes context.template.engine to nodes) are
        # still Django's observable semantics.
        template = self.template
        with context.render_context.push_state(template):
            if context.template is None:
                with context.bind_template(template):
                    context.template_name = template.name
                    return compiled(context)
            else:
                return compiled(context)
