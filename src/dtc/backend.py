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

from .runtime import compiled_for, template_render


class DTCTemplates(DjangoTemplates):
    """Drop-in replacement for the DjangoTemplates backend.

    Parsing, template loading, and configuration are inherited unchanged
    from Django; only the render path differs. Templates that the compiler
    can handle render through generated Python code, everything else falls
    back to Django's interpreted renderer.
    """

    def __init__(self, params):
        params = params.copy()
        options = params.get("OPTIONS", {}).copy()
        # dtc's own option must not reach Django's Engine (unknown kwargs
        # raise TypeError there).
        disk_cache = options.pop("dtc_disk_cache", None)
        params["OPTIONS"] = options
        super().__init__(params)
        if disk_cache:
            from .diskcache import resolve_dir

            self.engine._dtc_disk_cache = resolve_dir(disk_cache)

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
        self._compiled = compiled_for(template)  # instance-cached

    def render(self, context=None, request=None):
        context = make_context(
            context, request, autoescape=self.backend.engine.autoescape
        )
        # template_render reproduces django.template.base.Template.render
        # exactly around the compiled body (render_context push, template
        # binding — which runs context processors on RequestContext), and
        # falls back to Django's own render when not compiled.
        return template_render(self.template, context)
