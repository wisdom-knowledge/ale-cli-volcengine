"""Regression tests for the codex fork-binary overlay placement.

The overlay used to target two hardcoded absolute vendor paths. On the win10
image those missed (``%APPDATA%\\npm\\node_modules`` is not where the unpacked
Node installs globals), so ``replaced == 0`` and the run died with
``overlay did not yield pinned ... ; refusing to run a stale build`` — a silent
no-op on one side, a hard failure on the other.

These tests are offline: they build fake npm trees on tmp_path and drive the
real discovery helpers.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ale_run.agents.codex.deployer import (
    _VENDOR_LAYOUT,
    CodexDeployer,
    _vendor_candidates,
)


def _deployer() -> CodexDeployer:
    return CodexDeployer(
        SimpleNamespace(config=object(), sandbox=SimpleNamespace(is_linux=False))
    )


def _plant(root: Path, layout: str, os_key: str) -> Path:
    """Create a fake native binary at a given npm layout and return its path."""
    cli_pkg, plat_pkg, triple, binary = _VENDOR_LAYOUT[os_key]
    rel = Path("vendor") / triple / "codex" / binary
    if layout == "hoisted":
        pkg_dir = root / "@openai" / plat_pkg
    elif layout == "nested":
        pkg_dir = root / "@openai" / cli_pkg / "node_modules" / "@openai" / plat_pkg
    else:
        raise AssertionError(layout)
    target = pkg_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"fork-binary")
    return target


@pytest.mark.parametrize("os_key", ["windows", "linux"])
def test_candidates_cover_both_npm_layouts(tmp_path: Path, os_key: str) -> None:
    cli_pkg, plat_pkg, triple, binary = _VENDOR_LAYOUT[os_key]
    hoisted = _plant(tmp_path, "hoisted", os_key)
    nested = _plant(tmp_path, "nested", os_key)
    got = _vendor_candidates(str(tmp_path), cli_pkg, plat_pkg, triple, binary)
    assert got == [str(hoisted), str(nested)]


@pytest.mark.parametrize("os_key", ["windows", "linux"])
@pytest.mark.parametrize("layout", ["hoisted", "nested"])
async def test_discovers_binary_under_non_default_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, os_key: str, layout: str
) -> None:
    """The regression: a root that is NOT the hardcoded default still resolves.

    The old code only ever looked under ``%APPDATA%\\npm\\node_modules`` (or
    ``/usr/local/lib/node_modules``), so both of these were misses.
    """
    plant_root = tmp_path / "some-unexpected-root"
    planted = _plant(plant_root, layout, os_key)

    deployer = _deployer()

    async def fake_roots(_is_linux: bool) -> list[str]:
        return [str(plant_root)]

    monkeypatch.setattr(deployer, "_npm_global_roots", fake_roots)
    found = await deployer._vendor_binary_paths(is_linux=os_key == "linux")
    assert [os.path.normcase(p) for p in found] == [os.path.normcase(str(planted))]


async def test_returns_nothing_when_no_install_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployer = _deployer()

    async def fake_roots(_is_linux: bool) -> list[str]:
        return [str(tmp_path / "empty")]

    monkeypatch.setattr(deployer, "_npm_global_roots", fake_roots)
    assert await deployer._vendor_binary_paths(is_linux=False) == []


async def test_nested_preferred_over_hoisted_within_a_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """npm 11's nested copy is what ``bin/codex.js`` resolves, so it is tried first.

    Both layouts can coexist under one root after an npm upgrade; Node looks in
    the package's own ``node_modules`` before walking up, so the nested copy is
    the one that actually runs and the one the overlay must prefer.
    """
    root = tmp_path / "root-1"
    hoisted = _plant(root, "hoisted", "windows")
    nested = _plant(root, "nested", "windows")

    deployer = _deployer()

    async def fake_roots(_is_linux: bool) -> list[str]:
        return [str(root)]

    monkeypatch.setattr(deployer, "_npm_global_roots", fake_roots)
    found = await deployer._vendor_binary_paths(is_linux=False)
    assert found == [str(nested), str(hoisted)]


async def test_npm_root_g_wins_over_fallback_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real install lives under exactly one root; ``npm root -g`` names it.

    The trailing roots are heuristics, so a hit under the authoritative root
    must not be shadowed by one found under a guessed root.
    """
    authoritative = tmp_path / "from-npm-root"
    guessed = tmp_path / "guessed"
    real = _plant(authoritative, "nested", "windows")
    _plant(guessed, "nested", "windows")

    deployer = _deployer()

    async def fake_roots(_is_linux: bool) -> list[str]:
        return [str(authoritative), str(guessed)]

    monkeypatch.setattr(deployer, "_npm_global_roots", fake_roots)
    found = await deployer._vendor_binary_paths(is_linux=False)
    assert found[0] == str(real)


async def test_roots_always_include_npm_root_g_and_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``npm root -g`` is authoritative but not trusted alone."""
    deployer = _deployer()
    reported = str(tmp_path / "reported-by-npm")

    monkeypatch.setattr(
        "ale_run.agents.codex.deployer.shutil.which", lambda _name: None
    )

    class _Proc:
        returncode = 0
        stdout = reported + "\n"

    async def fake_to_thread(_fn, _cmd, **_kw):
        return _Proc()

    monkeypatch.setattr(
        "ale_run.agents.codex.deployer.asyncio.to_thread", fake_to_thread
    )
    roots = await deployer._npm_global_roots(is_linux=False)
    assert roots[0] == reported
    assert r"C:\Users\User\AppData\Roaming\npm\node_modules" in roots
