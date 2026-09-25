"""ScriptedGuiDeployer — a no-LLM GUI agent for ``demo/hello`` / ``demo/hello_win``.

Exercises the full pipeline (provision → cua bridge → GUI actions → output →
reference staging → evaluate → screenshots) without spending model tokens.

Per unit it connects to the eval VM over cua-server and:

  1. Screenshots the desktop.
  2. Runs the task's launcher (opens the Acme app in Chrome), screenshots.
  3. Types the Order ID from ``order.json`` into the app and presses
     Generate via the keyboard (Tab / type / Tab / Enter), screenshots.
  4. Writes the confirmation code to the task's ``result.txt``. The code is
     derived host-side with the app's own hash, so the task's evaluate()
     scores the run exactly as it would a real agent.

Screenshots are saved to ``work_dir/shots/`` and attached to the trajectory
in :meth:`parse_artifacts`; the framework's ``persist_screenshots`` writes
them to ``<run_dir>/screenshots/``.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, ClassVar

from ale_run.base_interface import (
    AgentRunResult,
    BaseAgentDeployer,
    TrajectoryBuilder,
)

from .config import ScriptedGuiConfig

logger = logging.getLogger(__name__)

_REPORT_NAME = "scripted_gui_report.json"
_SHOTS_DIR = "shots"

_PATH_RE = r"([A-Za-z]:\\\S+|/\S+)"


def confirmation_code(order_id: str) -> str:
    """Port of the Acme app's ``gen()`` (JS int32 / double semantics)."""

    def to_int32(v: int) -> int:
        v &= 0xFFFFFFFF
        return v - (1 << 32) if v & 0x80000000 else v

    h = 0
    for ch in order_id.strip():
        h = to_int32(to_int32(h) << 5) - h + ord(ch)
    h = to_int32(h) & 0x7FFFFFFF
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    b36 = ""
    while True:
        h, r = divmod(h, 36)
        b36 = digits[r] + b36
        if h == 0:
            break
    return "CONF-" + b36.upper().rjust(8, "0")[:8]


def _find_path(prompt: str, suffix: str) -> str:
    m = re.search(_PATH_RE.replace(r"\S+", r"\S*?" + re.escape(suffix)), prompt)
    if not m:
        raise ValueError(f"no path ending in {suffix!r} in the task prompt")
    return m.group(1)


class ScriptedGuiDeployer(BaseAgentDeployer):
    """Scripted GUI agent. Runs on the host, drives the eval VM."""

    default_executor: ClassVar[str] = "local"
    supported_executors: ClassVar[frozenset[str]] = frozenset({"local"})
    hot_artifacts: ClassVar[tuple[str, ...]] = (_REPORT_NAME,)

    @property
    def version(self) -> str | None:
        return "scripted-gui-0.1.0"

    async def install(self) -> None:
        from cua_bench.computers.remote import RemoteDesktopSession  # noqa: F401

        Path(self.executor.work_dir).mkdir(parents=True, exist_ok=True)

    async def launch(self, prompt: str) -> AgentRunResult:
        from cua_bench.computers.remote import RemoteDesktopSession

        cfg: ScriptedGuiConfig = self.config  # type: ignore[assignment]
        work_dir = Path(self.executor.work_dir)
        shots_dir = work_dir / _SHOTS_DIR
        shots_dir.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        sb = self.executor.sandbox
        report: dict[str, Any] = {"steps": [], "code": None}

        order_path = _find_path(prompt, "order.json")
        result_path = _find_path(prompt, "result.txt")
        launcher = _find_path(prompt, "launch.cmd" if sb.os == "windows" else "launch.sh")

        session = RemoteDesktopSession(
            api_url=sb.endpoint, os_type=sb.os, ephemeral=False, headless=True,
        )
        if not await session.wait_until_ready(timeout=cfg.connect_timeout_s):
            self._write_report(work_dir, report)
            return self._result("failed", t0, work_dir, error="VM cua-server not responsive")

        async def shot(label: str) -> None:
            png = await session.screenshot()
            name = f"{len(report['steps']):02d}_{label}.png"
            (shots_dir / name).write_bytes(png)
            report["steps"].append({"label": label, "screenshot": name})
            logger.info("scripted_gui: screenshot %s (%d bytes)", name, len(png))

        await shot("desktop")

        launch_cmd = f"cmd /c {launcher}" if sb.os == "windows" else f"bash {launcher}"
        await session.run_command(launch_cmd, check=False)
        await asyncio.sleep(cfg.app_load_wait_s)
        await shot("app_opened")

        order_id = json.loads(await session.read_file(order_path))["order_id"]
        await session.key("tab")
        await session.type(order_id)
        await session.key("tab")
        await session.key("enter")
        await asyncio.sleep(1.5)
        await shot("code_generated")

        code = confirmation_code(order_id)
        await session.write_file(result_path, code + "\n")
        report.update(code=code, order_id=order_id, result_path=result_path)
        await shot("done")

        self._write_report(work_dir, report)
        logger.info("scripted_gui: wrote %s to %s", code, result_path)
        return self._result("completed", t0, work_dir)

    @classmethod
    def parse_artifacts(
        cls,
        *,
        work_dir: Path,
        config: ScriptedGuiConfig,
        run_result: AgentRunResult,
        builder: TrajectoryBuilder,
    ) -> None:
        report_path = work_dir / _REPORT_NAME
        if not report_path.exists():
            builder.add_step(
                source="system",
                message=f"scripted_gui: report missing at {report_path}",
                extra={"reason": "no_report", "run_status": run_result.status},
            )
            return
        report = json.loads(report_path.read_text(encoding="utf-8"))
        for step in report["steps"]:
            png = (work_dir / _SHOTS_DIR / step["screenshot"]).read_bytes()
            builder.add_step(
                source="agent",
                message=f"scripted_gui: {step['label']}",
                extra={"_screenshot_b64": base64.b64encode(png).decode("ascii")},
            )
        builder.add_step(
            source="agent",
            message=f"scripted_gui: wrote {report.get('code')} to {report.get('result_path')}",
        )

    def _result(
        self, status: str, t0: float, work_dir: Path, *, error: str | None = None,
    ) -> AgentRunResult:
        return AgentRunResult(
            status=status,
            duration_s=time.monotonic() - t0,
            transcript_path=str(work_dir / _REPORT_NAME),
            error=error,
        )

    @staticmethod
    def _write_report(work_dir: Path, report: dict[str, Any]) -> None:
        (work_dir / _REPORT_NAME).write_text(json.dumps(report, indent=2), encoding="utf-8")
