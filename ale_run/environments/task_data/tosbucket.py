"""``task_data_source: tos://<bucket>`` — pull task data from a Volcengine TOS
bucket.

Mirror of :mod:`ale_run.environments.task_data.ossbucket`, using the in-box
``tosutil`` CLI.

Behavior (identical to the others):

* ``stage_input``: pull ``<tos_prefix>/input`` and ``<tos_prefix>/software`` to
  the sandbox. **Skip if already on the sandbox** (image-baked data intact).
* ``stage_reference``: always wipe + fresh sync. Reference is eval truth.

TOS auth: ECS instances launched by
:class:`~ale_run.environments.providers.volcengine.VolcengineProvider` carry an
**instance IAM role**, but ``tosutil`` has no instance-role credential mode
(unlike ``ossutil``'s ``EcsRamRole``). So every call goes through
:data:`_TOS_WRAPPER_PY`, written to the box on first use: it fetches the role's
STS credentials from the ECS metadata service and passes them to ``tosutil``
as per-command ``-i/-k/-t`` flags, with the region's internal endpoint
(``tos-<region>.ivolces.com``, no public egress). The image only needs
``tosutil`` on PATH.

``tosutil`` has no ``sync``; ``cp -r -u -flat`` is the equivalent (recursive,
incremental, contents of the prefix land directly in the destination dir).
"""
from __future__ import annotations

import logging
import shlex
from typing import Any

from ...base_interface import SandboxHandle, TaskDataSpec
from . import join, task_subdir

logger = logging.getLogger(__name__)

# Fetch the instance role's STS creds from ECS IMDS (IMDSv2 token → role name →
# credentials; paths from the official Volcengine SDK's ecs_role_provider.go),
# then exec ``tosutil <argv...> -i -k -t -re -e``.
_TOS_WRAPPER_PY = r'''
import json, subprocess, sys, urllib.request

IMDS = "http://100.96.0.96"

def _get(path, token):
    req = urllib.request.Request(IMDS + path, headers={"X-volc-ecs-metadata-token": token})
    return urllib.request.urlopen(req, timeout=5).read().decode()

region, argv = sys.argv[1], sys.argv[2:]
token = urllib.request.urlopen(urllib.request.Request(
    IMDS + "/latest/api/token", method="PUT",
    headers={"X-volc-ecs-metadata-token-ttl-seconds": "21600"}), timeout=5).read().decode()
roles = json.loads(_get("/volcstack/latest/iam/security_credentials?type=user&format=json", token))
role = roles[0] if isinstance(roles, list) else roles["RoleName"]
creds = json.loads(_get("/volcstack/latest/iam/security_credentials/" + role, token))
sys.exit(subprocess.call(["tosutil", *argv,
    "-i=" + creds["AccessKeyId"], "-k=" + creds["SecretAccessKey"],
    "-t=" + creds["SessionToken"],
    "-re=" + region, "-e=tos-" + region + ".ivolces.com"]))
'''


async def stage_input(
    sandbox: SandboxHandle, task_data: TaskDataSpec, *, source: str,
) -> dict[str, Any]:
    tos_prefix = _tos_prefix(source, task_data)
    base = task_subdir(sandbox, task_data)
    await sandbox.mkdir(base)

    staged: list[str] = []
    for subdir in ("input", "software"):
        dst = join(sandbox, base, subdir)
        if await _has_baked_files(sandbox, dst):
            logger.info("tosbucket: %s already present on sandbox, skipping sync", dst)
            staged.append(f"{subdir}(baked)")
            continue
        src = f"{tos_prefix}/{subdir}"
        if not await _tos_exists(sandbox, src):
            await sandbox.mkdir(dst)
            continue
        await sandbox.mkdir(dst)
        r = await tosutil(sandbox, "cp", _dir_url(src), dst, "-r", "-f", "-u", "-flat",
                          timeout=600)
        if r.returncode != 0:
            raise RuntimeError(
                f"tosutil cp {subdir} failed (rc={r.returncode}): "
                f"{(r.stderr or r.stdout or '')[:300]}"
            )
        if subdir == "software" and sandbox.is_linux:
            await sandbox.run_command(
                f"find {shlex.quote(dst)} -type f -exec chmod +x {{}} +",
                timeout=60,
            )
        staged.append(subdir)

    await sandbox.mkdir(join(sandbox, base, "output"))
    return {"staged": staged, "source": source}


async def stage_reference(
    sandbox: SandboxHandle, task_data: TaskDataSpec, *, source: str,
) -> dict[str, Any]:
    tos_prefix = _tos_prefix(source, task_data)
    base = task_subdir(sandbox, task_data)
    src = f"{tos_prefix}/reference"
    dst = join(sandbox, base, "reference")

    if not await _tos_exists(sandbox, src):
        return {"skipped": True, "reason": "no_reference_on_tos"}

    await sandbox.rm([dst])
    await sandbox.mkdir(dst)
    r = await tosutil(sandbox, "cp", _dir_url(src), dst, "-r", "-f", "-flat", timeout=600)
    if r.returncode != 0:
        raise RuntimeError(
            f"tosutil cp reference failed (rc={r.returncode}): "
            f"{(r.stderr or r.stdout or '')[:300]}"
        )
    # tosutil does not preserve POSIX mode bits; normalize like gsbucket so
    # grading sees predictable perms.
    if sandbox.is_linux:
        await sandbox.run_command(f"chmod -R 777 {shlex.quote(dst)}", timeout=60)
    return {"staged": ["reference"], "source": source}


async def tosutil(sandbox: SandboxHandle, *argv: str, timeout: int) -> Any:
    """Run ``tosutil <argv...>`` in the box under the instance role's STS creds.

    Shared with :func:`ale_run.environments.output_pull.push_to_tos`."""
    region = sandbox.metadata.get("region")
    if not region:
        raise RuntimeError(
            "tos:// needs the sandbox's Volcengine region (handle metadata `region`); "
            "only VolcengineProvider sandboxes can reach TOS"
        )
    wrapper = join(sandbox, sandbox.work_dir_base, "_tos_wrapper.py")
    if not await sandbox.exists(wrapper):
        await sandbox.write_file(wrapper, _TOS_WRAPPER_PY)
    if sandbox.is_linux:
        cmd = shlex.join([sandbox.python, wrapper, region, *argv])
    else:
        cmd = " ".join(f'"{a}"' for a in (sandbox.python, wrapper, region, *argv))
    return await sandbox.run_command(cmd, timeout=timeout)


# ---- helpers ----


def _tos_prefix(source: str, task_data: TaskDataSpec) -> str:
    return (
        f"{source.rstrip('/')}/{task_data.domain_name}/"
        f"{task_data.task_name}/{task_data.variant_name}"
    )


def _dir_url(url: str) -> str:
    return url.rstrip("/") + "/"


async def _has_baked_files(sandbox: SandboxHandle, path: str) -> bool:
    if not await sandbox.exists(path):
        return False
    entries = await sandbox.list_dir(path)
    return any(not e["is_dir"] for e in entries)


async def _tos_exists(sandbox: SandboxHandle, tos_url: str) -> bool:
    """True if the prefix has at least one object.

    Like ``ossutil ls``, ``tosutil ls`` exits 0 on an empty prefix, so ask for
    one object in short form (``-s``: one ``tos://`` url per line) and look for
    an object line under the prefix."""
    url = _dir_url(tos_url)
    r = await tosutil(sandbox, "ls", url, "-s", "-limit=1", timeout=30)
    if r.returncode != 0:
        return False
    return any(line.strip().startswith(url) for line in (r.stdout or "").splitlines())
