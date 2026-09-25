"""ScriptedGuiConfig — knobs for the scripted GUI agent."""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


@dataclass
class ScriptedGuiConfig:
    """Tunables for :class:`ScriptedGuiDeployer`."""

    name: ClassVar[str] = "scripted_gui"

    model: str = "none"
    """Unused. The scripted agent makes no model calls."""

    connect_timeout_s: int = 120
    """Seconds to wait for the eval VM's cua-server to become responsive."""

    app_load_wait_s: float = 8.0
    """Seconds to wait after launching the app before the next screenshot."""
