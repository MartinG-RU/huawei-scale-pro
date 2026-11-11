"""Shared fixtures.

The Home Assistant facing modules (``__init__``, coordinator, config flow, …)
are not imported here: these tests exercise the pure-Python protocol, session
and model code, so they run without a Home Assistant install. Importing the
package would execute its ``__init__`` (which needs Home Assistant), so a bare
namespace module is registered for the package first.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

_PACKAGE = "custom_components.huawei_scale_pro"
_PACKAGE_PATH = Path(__file__).resolve().parent.parent / "custom_components" / "huawei_scale_pro"

if _PACKAGE not in sys.modules:
    package = types.ModuleType(_PACKAGE)
    package.__path__ = [str(_PACKAGE_PATH)]  # type: ignore[attr-defined]
    sys.modules[_PACKAGE] = package

import pytest  # noqa: E402

from custom_components.huawei_scale_pro import session  # noqa: E402


@pytest.fixture(autouse=True)
def fast_session_timers(monkeypatch):
    """Shrink every session timeout so simulated weigh-ins take milliseconds."""
    monkeypatch.setattr(session, "STATUS_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(session, "AUTH_TIMEOUT", 0.2)
    monkeypatch.setattr(session, "MANAGER_INFO_TIMEOUT", 0.2)
    monkeypatch.setattr(session, "USER_INFO_ACK_TIMEOUT", 0.2)
    monkeypatch.setattr(session, "HISTORY_RECORD_TIMEOUT", 0.1)
    monkeypatch.setattr(session, "FINAL_REQUEST_GRACE", 0.1)
    monkeypatch.setattr(session, "MEASUREMENT_TIMEOUT", 1.0)
    monkeypatch.setattr(session, "MAX_STATUS_POLLS", 5)
