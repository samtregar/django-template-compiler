"""Engine-level instrumentation: compile every ``django.template.base.Template``.

``install()`` patches ``Template._render`` to try the dtc-compiled path with
lazy per-instance compilation, falling back to Django's interpreted renderer.
This hooks the *engine* level rather than the BACKENDS proxy, so it also
covers templates constructed directly — which is how Django's own
``template_tests`` suite builds them (see ``scripts/run_django_suite.py``).

It also substitutes ``django.test.utils.instrumented_test_render`` so that
when ``setup_test_environment()`` re-patches ``_render`` for test
instrumentation, the replacement still takes the compiled path: the
``template_rendered`` signal is sent exactly as stock Django sends it, then
rendering proceeds compiled-or-fallback.

Experimental; the supported integration point is ``dtc.backend.DTCTemplates``.
"""

from __future__ import annotations

from .compiler import compile_template

_MISSING = object()
_installed = False

#: Render/compile counters, so suite runs can prove the compiled path was
#: actually exercised rather than silently falling back everywhere.
stats = {
    "templates_compiled": 0,
    "templates_fallback": 0,
    "renders_compiled": 0,
    "renders_fallback": 0,
}


def _compiled_for(template):
    compiled = template.__dict__.get("_dtc_compiled", _MISSING)
    if compiled is _MISSING:
        compiled = compile_template(template)  # fail-open: returns None on error
        template._dtc_compiled = compiled
        key = "templates_compiled" if compiled is not None else "templates_fallback"
        stats[key] += 1
    return compiled


def install():
    global _installed
    if _installed:
        return
    _installed = True

    import django.test.utils as test_utils
    from django.template.base import Template
    from django.test.signals import template_rendered

    orig_render = Template._render

    def _render(self, context):
        compiled = _compiled_for(self)
        if compiled is None:
            stats["renders_fallback"] += 1
            return orig_render(self, context)
        stats["renders_compiled"] += 1
        return compiled(context)

    Template._render = _render

    def instrumented_test_render(self, context):
        # Byte-for-byte what django.test.utils.instrumented_test_render does,
        # with the nodelist render swapped for compiled-or-fallback.
        template_rendered.send(sender=self, template=self, context=context)
        compiled = _compiled_for(self)
        if compiled is None:
            stats["renders_fallback"] += 1
            return self.nodelist.render(context)
        stats["renders_compiled"] += 1
        return compiled(context)

    test_utils.instrumented_test_render = instrumented_test_render
