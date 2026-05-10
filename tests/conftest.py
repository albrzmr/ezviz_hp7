"""Test setup for the ezviz_hp7 fork.

Runs pure-Python unit tests against modules that do not pull
``homeassistant.*`` at import time — ``stats.py``, the ``cpd7/``
subpackage, ``pylocalapi/cas.py`` helpers etc.  HA-dependent platform
modules are out of scope for now and would need
``pytest-homeassistant-custom-component`` plumbing.

Importing ``custom_components.ezviz_hp7.cpd7`` still triggers
``custom_components.ezviz_hp7.__init__``, which pulls a handful of
``homeassistant.*`` modules.  We stub them here just enough to satisfy
the imports — we never call into them from these unit tests.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

# Add the repo root to sys.path so tests can do
# ``from custom_components.ezviz_hp7.stats import ActivityStats``.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# Pre-register the ``custom_components.ezviz_hp7`` package as a stub so
# importing submodules (``cpd7``, ``stats``) does NOT execute the real
# ``__init__.py`` — which pulls a HA dependency chain (``api.py`` →
# ``pylocalapi`` → modern ``str | int`` union syntax) that's heavy and
# Python-3.10+ only.  By placing a stub with ``__path__`` set, Python's
# import machinery still finds the submodules via the filesystem.
def _ensure_stub_package(name: str, path: Path) -> None:
    if name in sys.modules:
        return
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(path)]
    sys.modules[name] = pkg


_CC_PATH = _ROOT / "custom_components"
_ENT_PATH = _CC_PATH / "ezviz_hp7"
_ensure_stub_package("custom_components", _CC_PATH)
_ensure_stub_package("custom_components.ezviz_hp7", _ENT_PATH)
# NOTE: do NOT stub ``custom_components.ezviz_hp7.cpd7`` — let its real
# ``__init__.py`` run so ``StreamDecoder`` / ``Cpd7LanClient`` are
# exported.  Only the *parent* package needs to be stubbed.


def _stub(name: str, **attrs: object) -> types.ModuleType:
    mod = sys.modules.get(name) or types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


# homeassistant stubs — only needed if a test imports something that
# *transitively* reaches a HA module.  The pre-packaged stubs above
# bypass the ezviz_hp7 ``__init__.py``, so plain ``cpd7`` / ``stats``
# tests don't need any HA stubs.  Leave this function available for
# future tests that might.
def _stub(name: str, **attrs: object) -> types.ModuleType:
    mod = sys.modules.get(name) or types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod
