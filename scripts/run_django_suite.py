#!/usr/bin/env python
"""Run Django's own test suite against dtc's compiled render path.

Usage:
    run_django_suite.py /path/to/django-checkout [runtests.py args...]

Default args are ``template_tests --parallel=1``. The checkout must match the
installed Django version (verified below), e.g.:

    git clone --depth 1 --branch "$(python -c 'import django; print(django.__version__)')" \\
        https://github.com/django/django.git

This is the project's compatibility oracle: Django's suite runs with
``dtc.autopatch`` installed, so every template render tries the compiled path
first. Stats printed at exit prove the compiled path was exercised.
"""

import atexit
import os
import re
import runpy
import sys


def checkout_version(checkout):
    init = os.path.join(checkout, "django", "__init__.py")
    with open(init) as f:
        match = re.search(r"VERSION = \((\d+), (\d+), (\d+)", f.read())
    return tuple(int(g) for g in match.groups())


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    checkout = os.path.abspath(sys.argv[1])
    extra = sys.argv[2:] or ["template_tests", "--parallel=1"]

    import django

    if checkout_version(checkout) != django.VERSION[:3]:
        sys.exit(
            f"Django checkout at {checkout} is {checkout_version(checkout)}, "
            f"but installed Django is {django.VERSION[:3]}; they must match."
        )

    import dtc.autopatch

    dtc.autopatch.install()

    @atexit.register
    def report():
        print(f"dtc autopatch stats: {dtc.autopatch.stats}", file=sys.stderr)
        if not dtc.autopatch.stats["renders_compiled"]:
            print(
                "dtc: WARNING: no renders took the compiled path; "
                "the suite proved nothing about dtc.",
                file=sys.stderr,
            )

    runtests = os.path.join(checkout, "tests", "runtests.py")
    # runtests.py expects its own directory on sys.path (test_sqlite settings,
    # test app modules); running via runpy doesn't add it automatically.
    sys.path.insert(0, os.path.dirname(runtests))
    sys.argv = [runtests, *extra]
    runpy.run_path(runtests, run_name="__main__")


if __name__ == "__main__":
    main()
