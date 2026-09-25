"""Offline tests for the scripted GUI agent's host-side helpers."""
from __future__ import annotations

import pytest

from ale_run.agents.scripted_gui.deployer import _find_path, confirmation_code
from tasks.demo.hello_win.main import TaskConfig


def test_confirmation_code_matches_app_hash():
    # Value produced by the Acme app's JS gen() in node for this Order ID.
    assert confirmation_code("ORD-2024-WIN-DEMO-001") == "CONF-008CKY55"


def test_find_path_reads_hello_win_prompt():
    cfg = TaskConfig()
    prompt = cfg.task_description
    assert _find_path(prompt, "order.json") == cfg.order_path
    assert _find_path(prompt, "result.txt") == cfg.result_path
    assert _find_path(prompt, "launch.cmd") == cfg.launcher_path


def test_find_path_missing_raises():
    with pytest.raises(ValueError, match="order.json"):
        _find_path("no paths here", "order.json")
