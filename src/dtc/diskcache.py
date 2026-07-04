"""Disk cache of compiled template code objects (cold-start optimization).

What's cached is the *code object* produced by ``compile()`` of the
generated source — not the compiled function: the function's namespace
holds live objects from the parse (nodes, FilterExpressions, registered
filter functions) and is rebuilt by codegen on every process start. Codegen
is cheap string-building; ``compile()`` is the expensive step this skips.

Entries are keyed by the SHA-256 of the generated source, which makes the
cache self-validating: the source reflects everything behavior-relevant —
the template source, the engine's libraries/builtins (they change the
parse and the emitted filter specializations), and dtc's codegen — so
equal source means an equivalent code object. The directory name embeds
the Python and dtc versions (marshal is Python-version-specific; namespace
helper semantics are dtc-version-specific). One cosmetic exception: the
``<dtc:name>`` filename baked into the code object comes from whichever
template compiled first among identical sources.

Loading is fail-open everywhere: a missing, corrupt, or truncated entry
means a fresh ``compile()`` (and a rewrite). Because loaded code is
``exec``d, the cache directory must be trusted — the ``True`` default
resolves under the user's cache home (created ``0700``), never the shared
tempdir. Opt in per engine (``OPTIONS["dtc_disk_cache"]``) or globally
(``DTC_DISK_CACHE=<path>``).
"""

from __future__ import annotations

import hashlib
import marshal
import os
import sys
import tempfile

from . import __version__

_TAG = f"py{sys.version_info[0]}.{sys.version_info[1]}-dtc{__version__}"


def resolve_dir(option):
    """Turn the engine option (True or a path) into a versioned, existing
    cache directory."""
    if option is True:
        base = os.environ.get(
            "XDG_CACHE_HOME", os.path.join(os.path.expanduser("~"), ".cache")
        )
        option = os.path.join(base, "dtc")
    path = os.path.join(os.path.expanduser(option), _TAG)
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def directory_for(template):
    """The cache directory for this template's engine, or None."""
    configured = getattr(template.engine, "_dtc_disk_cache", None)
    if configured:
        return configured
    env = os.environ.get("DTC_DISK_CACHE")
    if env:
        try:
            return resolve_dir(env)
        except OSError:
            return None
    return None


def _entry_path(directory, source):
    return os.path.join(
        directory, hashlib.sha256(source.encode()).hexdigest() + ".marshal"
    )


def load(directory, source):
    """The cached code object for this generated source, or None."""
    from .runtime import stats

    try:
        with open(_entry_path(directory, source), "rb") as f:
            code = marshal.loads(f.read())
    except (OSError, ValueError, EOFError, TypeError):
        stats["disk_misses"] += 1
        return None
    stats["disk_hits"] += 1
    return code


def store(directory, source, code):
    """Atomically write the code object; failures are silent (the cache is
    an optimization, never a requirement)."""
    path = _entry_path(directory, source)
    try:
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(marshal.dumps(code))
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
    except OSError:
        pass
