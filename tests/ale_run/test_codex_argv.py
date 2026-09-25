"""``codex exec`` argv must run in a non-git work dir without calling git.

The win10 image has no ``git`` on PATH, so the former ``git init`` in
``launch`` died with ``FileNotFoundError: [WinError 2]`` before codex ever
spawned. ``--skip-git-repo-check`` replaces it.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ale_run.agents.codex.config import CodexConfig
from ale_run.agents.codex.deployer import CodexDeployer


@pytest.mark.parametrize("yolo", [True, False])
def test_argv_skips_git_repo_check(yolo: bool) -> None:
    cfg = CodexConfig(model="gpt-test", yolo=yolo)
    deployer = CodexDeployer(SimpleNamespace(config=cfg))
    deployer._codex_path = r"C:\node\codex.cmd"

    argv = deployer._build_argv(cfg)

    assert argv[:2] == [r"C:\node\codex.cmd", "exec"]
    assert argv[argv.index("--model") + 1] == "gpt-test"
    assert "--skip-git-repo-check" in argv
    assert "git" not in argv
