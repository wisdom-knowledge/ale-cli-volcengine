"""Scripted GUI agent — no-LLM end-to-end check for the demo hello tasks.

Drives the Acme app over cua-server (launch, click, type, screenshot) and
writes the confirmation code, so the task's real evaluate() can score it.
"""

from .config import ScriptedGuiConfig
from .deployer import ScriptedGuiDeployer

__all__ = ["ScriptedGuiConfig", "ScriptedGuiDeployer"]
