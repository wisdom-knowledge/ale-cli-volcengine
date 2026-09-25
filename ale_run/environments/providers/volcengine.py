"""VolcengineProvider — ephemeral Volcengine ECS instances via ``ve ecs
RunInstances``.

Adapted from :mod:`ale_run.environments.providers.aliyun`, targeting Volcengine
(火山引擎) Cloud:

* **Framework facts (hardcoded, top of file)**: default instance types, the
  C→G instance fallback, retry tuning, error classification.
* **Deployment knobs (yaml ``provider.config`` → :class:`VolcengineProviderConfig`)**:
  region, security group, optional key pair + IAM role, instance_prefix,
  internet bandwidth, system-disk type + size, and the ``snapshots`` map (logical
  tag → custom image + optional gpu + zones).

A task asks for a logical snapshot (``cpu-free-ubuntu`` / ...); the provider
resolves it via the yaml ``snapshots`` map to a custom-image id + a zone list,
picks an instance type (task-card ``vm.machineType`` override, else a default,
with C→G family fallback), and tries the zones in order on capacity errors.
Each zone is resolved to a Subnet in that zone within the security group's VPC.

Every call goes through the ``ve`` CLI (``npm i -g @volcengine/cli``, >= 1.1.11).
ECS/VPC/IAM actions are not bundled in ``ve``'s local metadata, so each call
passes ``--force`` with a pinned ``--version`` and ``--endpoint``; parameters are
the raw OpenAPI names (PascalCase, ``.N`` array indices from 1). Errors land on
stderr as ``<Code>: <message>``.

Credentials: the provider uses the ambient ``ve`` credential chain (``ve
configure`` profile / ``VOLCENGINE_ACCESS_KEY`` + ``VOLCENGINE_SECRET_KEY`` env).
The in-box ``tosutil`` authenticates to TOS via the instance **IAM role**,
associated right after launch (Volcengine has no launch-time role parameter) —
so there is nothing to inject for tos:// task data / output.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from ...base_interface import Provider, ReleaseMode, SandboxHandle, SandboxSpec

# Cloud-agnostic helpers shared with the gcloud/aws/aliyun providers.
from .gcloud import (
    _SET_RES_PY,
    _init_computer_skip_wait,
    _parse_gce_machine_type,
    sanitize_label_value,
    wait_cua_ready,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Configuration — framework facts (hardcoded). Snapshot→image + zones are
# deployment-specific and live in the yaml profile (VolcengineProviderConfig).
# ============================================================================

# Default instance when a task_card declares no ``vm.machineType``. CPU falls
# back C→G (see _instance_chain); GPU has no instance fallback.
_DEFAULT_CPU_INSTANCE = "ecs.g4i.2xlarge"          # 8 vCPU / 32 GiB, general
_DEFAULT_GPU_INSTANCE = "ecs.gni2.8xlarge"         # A10 GPU instance

# Launch retry tuning.
_VOLCENGINE_MAX_RETRIES_TRANSIENT = 3
_VOLCENGINE_TRANSIENT_BASE_DELAY = 15          # seconds, exponential backoff
# Pinned OpenAPI versions per service (``--force`` requires an explicit one).
_API_VERSIONS = {"ecs": "2020-04-01", "vpc": "2020-04-01", "iam": "2018-01-01"}
_API_ENDPOINT = "open.volcengineapi.com"

# Delete retry tuning.
_DELETE_MAX_RETRIES = 6
_DELETE_RETRY_DELAY = 12                       # seconds

# error-code/message substring → error class. transient = retry same zone;
# zone = move to next zone (capacity); anything else = fail fast.
_VOLCENGINE_RETRYABLE_TRANSIENT = [
    "throttling", "flowlimitexceeded", "requestlimitexceeded", "rate exceeded",
    "internalerror", "internalservicetimeout", "serviceunavailable", "servicetimeout",
]
_TRANSIENT_TRANSPORT = [
    "connection reset", "connection refused", "timed out", "timeout",
    "could not connect", "connection aborted", "i/o timeout", "no such host",
]
_VOLCENGINE_RETRYABLE_ZONE = [
    "nostock", "out of stock", "insufficientinventory", "insufficientcapacity",
    "resourcenotenough", "resource.notenough", "resourcenotavailable",
    "invalidinstancetype.notfound", "zonenotsupported",
    "no capacity", "not have capacity",
]


# ============================================================================
# Provider config
# ============================================================================


@dataclass(frozen=True)
class SnapshotConfig:
    """What a logical snapshot tag maps to (yaml ``snapshots.<tag>``).

    ``image`` is the **image-family name** (the same registry key gcloud/aws/aliyun
    use — ``ale-ubuntu22`` / ``ale-win10`` — from :mod:`ale_run.environments.images`),
    NOT a raw image id. The provider resolves that family to a concrete custom
    image id at acquire time by looking up a self-owned image tagged
    ``ale:image-family=<name>``.

    ``zones`` holds **Volcengine zone ids** (e.g. ``cn-beijing-a``) to try in order
    on capacity errors. The provider maps each zone to a Subnet in that zone within
    the security group's VPC.
    """

    image: str               # image-family name (registry key), e.g. "ale-win10"
    gpu: str | None          # truthy → use a GPU instance type; None for CPU
    zones: tuple[str, ...]   # zone ids to try, in order, on capacity errors
    image_id: str | None = None   # optional explicit image id override
    resolution: tuple[int, int] | None = None
    """Windows display resolution (w, h) forced after boot."""

    @property
    def os(self) -> str:
        return "windows" if "win" in self.image.lower() else "linux"


@dataclass(frozen=True)
class VolcengineProviderConfig:
    """volcengine provider config (yaml ``provider.config``).

        region                    Volcengine region id, e.g. ``cn-beijing``
        security_group            the firewall — a security-group NAME or id
        instance_prefix           instance Name prefix
        key_name                  ECS key pair name (optional)
        iam_role_name             instance IAM role granting in-box TOS access
        internet_max_bandwidth    EIP bandwidth in Mbit/s. > 0 allocates a
                                  pay-by-traffic EIP released with the instance
                                  (default 100); 0 keeps the box private
        system_disk_category      system-disk type for the boot volume (default
                                  ``ESSD_PL0``)
        system_disk_size          system-disk size in GiB (default 100); must be
                                  >= the image's size
        instance_charge_type      ``PostPaid`` (pay-as-you-go, default) or ``PrePaid``
        snapshots                 dict[snapshot tag → SnapshotConfig]

    Credentials are NOT here — the provider uses the ambient ``ve`` credential
    chain (``ve configure`` / ``VOLCENGINE_ACCESS_KEY`` + ``VOLCENGINE_SECRET_KEY``).
    """

    region: str
    security_group: str = "ale-sandbox"
    instance_prefix: str = "ale"
    key_name: str = ""
    iam_role_name: str = ""
    internet_max_bandwidth: int = 100
    system_disk_category: str = "ESSD_PL0"
    system_disk_size: int = 100
    instance_charge_type: str = "PostPaid"
    output_to_bucket: bool = False
    """Set by the config loader when ``output_path`` is a ``tos://`` bucket."""
    snapshots: dict[str, SnapshotConfig] = dataclass_field(default_factory=dict)

    @property
    def associate_public_ip(self) -> bool:
        return self.internet_max_bandwidth > 0


def _build_snapshot_config(raw: Any) -> SnapshotConfig:
    if not isinstance(raw, dict):
        raise TypeError(f"snapshot entry must be a mapping, got {type(raw).__name__}")
    image = raw.get("image")
    if not image:
        raise KeyError(
            f"snapshot entry missing required `image` (image-family name, "
            f"e.g. ale-win10): {raw!r}"
        )
    zones = tuple(raw.get("zones") or ())
    if not zones:
        raise KeyError(f"snapshot {image!r} missing required `zones` (zone ids)")
    return SnapshotConfig(
        image=str(image), gpu=raw.get("gpu"), zones=zones,
        image_id=(str(raw["image_id"]) if raw.get("image_id") else None),
        resolution=_parse_resolution(raw.get("resolution"), image),
    )


def _parse_resolution(raw: Any, image: str) -> tuple[int, int] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        parts = raw.lower().replace(" ", "").split("x")
    elif isinstance(raw, (list, tuple)):
        parts = list(raw)
    else:
        raise TypeError(
            f"snapshot {image!r} resolution must be [w, h] or 'WxH', got {raw!r}"
        )
    if len(parts) != 2:
        raise ValueError(f"snapshot {image!r} resolution must have 2 values, got {raw!r}")
    try:
        return (int(parts[0]), int(parts[1]))
    except (TypeError, ValueError):
        raise ValueError(f"snapshot {image!r} resolution values must be ints, got {raw!r}")


def _build_provider_config(raw: dict[str, Any]) -> VolcengineProviderConfig:
    snapshots = {
        str(tag): _build_snapshot_config(entry)
        for tag, entry in (raw.get("snapshots") or {}).items()
    }
    return VolcengineProviderConfig(
        region=str(raw["region"]),
        security_group=str(raw.get("security_group") or "ale-sandbox"),
        instance_prefix=str(raw.get("instance_prefix") or "ale"),
        key_name=str(raw.get("key_name") or ""),
        iam_role_name=str(raw.get("iam_role_name") or ""),
        internet_max_bandwidth=int(raw.get("internet_max_bandwidth", 100)),
        system_disk_category=str(raw.get("system_disk_category") or "ESSD_PL0"),
        system_disk_size=int(raw.get("system_disk_size", 100)),
        instance_charge_type=str(raw.get("instance_charge_type") or "PostPaid"),
        output_to_bucket=bool(raw.get("output_to_bucket", False)),
        snapshots=snapshots,
    )


# ============================================================================
# Instance-type fallback chain
# ============================================================================


def _cpu_family_fallback(instance_type: str) -> str | None:
    """C-family → G fallback, keeping the size suffix.

    ``ecs.c4i.2xlarge`` → ``ecs.g4i.2xlarge``. Returns None for non-C families.
    """
    m = re.fullmatch(r"(ecs\.)c(\w*?)(\..+)", instance_type)
    if m:
        return f"{m.group(1)}g{m.group(2)}{m.group(3)}"
    return None


def _instance_chain(instance_type: str, *, is_gpu: bool) -> tuple[str, ...]:
    """Ordered instance types to try for one box.

    * GPU: just the requested type — no fallback.
    * CPU: the requested type, then its G fallback (``ecs.c*`` → ``ecs.g*``).
    """
    if is_gpu:
        return (instance_type,)
    fb = _cpu_family_fallback(instance_type)
    return (instance_type, fb) if fb else (instance_type,)


# ecs.g4i is a 1:4 vCPU:GiB general-purpose family, which matches
# the GCE ``*-standard-*`` ratio closely enough for every task shape we run.
# (Not g3i: public Windows images reject it with InvalidImage.InstanceTypeMismatch.)
# vCPU count → ecs.g4i size suffix.
_ECS_G4I_SIZES: tuple[tuple[int, str], ...] = (
    (2, "large"), (4, "xlarge"), (8, "2xlarge"), (16, "4xlarge"),
    (32, "8xlarge"), (64, "16xlarge"),
)


def _resolve_instance_type(machine_type: str | None, *, is_gpu: bool) -> str:
    """Concrete ecs instance type for a task card's ``vm.machineType``.

    Task cards carry GCE-style names (``c4-standard-4``, ``g2-standard-8``) —
    the same cards run on every provider. Translate to a Volcengine type:

    * ``None`` → the CPU/GPU default.
    * an ``ecs.*`` string → used verbatim (an explicit Volcengine override).
    * a GCE-style name → parsed to a vCPU count and mapped to the matching
      ``ecs.g4i`` size.
    """
    default = _DEFAULT_GPU_INSTANCE if is_gpu else _DEFAULT_CPU_INSTANCE
    if not machine_type:
        return default
    if machine_type.startswith("ecs."):
        return machine_type
    shape = _parse_gce_machine_type(machine_type)
    if shape is None:
        logger.warning(
            "unparseable machineType %r — using default %s", machine_type, default
        )
        return default
    if is_gpu:
        return _DEFAULT_GPU_INSTANCE
    size = next(
        (s for cap, s in _ECS_G4I_SIZES if shape.vcpus <= cap), _ECS_G4I_SIZES[-1][1]
    )
    return f"ecs.g4i.{size}"


# ============================================================================
# Naming
# ============================================================================


def generate_instance_name(
    prefix: str,
    *,
    snapshot: str,
    task_id: str = "",
    harness: str = "",
    model_tag: str = "",
) -> str:
    """``<prefix>-<task-or-snapshot>-<hash8>`` instance name."""
    if task_id:
        body = re.sub(r"[^a-z0-9]", "-", task_id.lower()).strip("-")[:40]
    else:
        body = re.sub(r"[^a-z0-9]", "-", snapshot.lower()).strip("-")[:30]
    seed = f"{prefix}:{task_id}:{harness}:{model_tag}:{snapshot}:{time.time()}:{random.random()}"
    h = hashlib.sha256(seed.encode()).hexdigest()[:8]
    name = f"{prefix}-{body}-{h}"[:128]
    return name if name[:1].isalpha() else f"a{name}"[:128]


# ============================================================================
# volcengine CLI wrapper + error classification
# ============================================================================


async def _run_ve(
    service: str, action: str, region: str, *params: str,
) -> tuple[int, str, str]:
    """``ve <service> <action> <params...>`` against ``region`` → (rc, stdout, stderr).

    ``params`` are raw OpenAPI parameters (``--InstanceIds.1 i-xxx``); the
    system flags ``--force`` needs are appended here.
    """
    cmd = [
        "ve", service, action, *params,
        "--region", region,
        "--version", _API_VERSIONS[service],
        "--endpoint", _API_ENDPOINT,
        "--force",
        "--output", "json",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout_b.decode(errors="replace"),
        stderr_b.decode(errors="replace"),
    )


def _error_code(stderr: str) -> str:
    """Lower-cased API error code — ``ve`` prints ``<Code>: <message>`` first."""
    m = re.match(r"\s*([A-Za-z][A-Za-z0-9_.]*):\s", stderr)
    return m.group(1).lower() if m else ""


def _is_transient_error(stderr: str) -> bool:
    code = _error_code(stderr)
    if code:
        return any(pat in code for pat in _VOLCENGINE_RETRYABLE_TRANSIENT)
    lower = stderr.lower()
    return any(pat in lower for pat in _TRANSIENT_TRANSPORT)


def _is_zone_capacity_error(stderr: str) -> bool:
    code = _error_code(stderr)
    haystack = code if code else stderr.lower()
    return any(pat in haystack for pat in _VOLCENGINE_RETRYABLE_ZONE)


# ============================================================================
# RunInstances argv
# ============================================================================


def _build_run_args(
    *,
    name: str,
    image_id: str,
    instance_type: str,
    zone_id: str,
    subnet_id: str,
    security_group_id: str,
    cfg: VolcengineProviderConfig,
    snapshot_tag: str,
) -> list[str]:
    """``ve ecs RunInstances`` API params (ECS OpenAPI 2020-04-01).

    Subnet + security group ride on the primary ``NetworkInterfaces.1``. Without
    a key pair the image's baked credential is kept (RunInstances rejects a
    launch with no Password / KeyPairName / KeepImageCredential).
    """
    args = [
        "--ZoneId", zone_id,
        "--ImageId", image_id,
        "--InstanceTypeId", instance_type,
        "--InstanceChargeType", cfg.instance_charge_type,
        "--InstanceName", name,
        "--NetworkInterfaces.1.SubnetId", subnet_id,
        "--NetworkInterfaces.1.SecurityGroupIds.1", security_group_id,
        "--Volumes.1.VolumeType", cfg.system_disk_category,
        "--Volumes.1.Size", str(cfg.system_disk_size),
        "--Volumes.1.DeleteWithInstance", "true",
        "--Tags.1.Key", "purpose",
        "--Tags.1.Value", "ale-run",
        "--Tags.2.Key", "snapshot",
        "--Tags.2.Value", sanitize_label_value(snapshot_tag),
    ]
    if cfg.associate_public_ip:
        args += [
            "--EipAddress.BandwidthMbps", str(cfg.internet_max_bandwidth),
            "--EipAddress.ChargeType", "PayByTraffic",
            "--EipAddress.ReleaseWithInstance", "true",
        ]
    if cfg.key_name:
        args += ["--KeyPairName", cfg.key_name]
    else:
        args += ["--KeepImageCredential", "true"]
    return args


async def _try_run_in_zone(
    *,
    name: str,
    image_id: str,
    instance_type: str,
    zone_id: str,
    subnet_id: str,
    security_group_id: str,
    cfg: VolcengineProviderConfig,
    snapshot_tag: str,
) -> tuple[bool, str, str]:
    """Returns (ok, stdout, stderr). On capacity error returns ok=False without
    retrying (caller moves to the next zone); transient errors retry here."""
    args = _build_run_args(
        name=name,
        image_id=image_id,
        instance_type=instance_type,
        zone_id=zone_id,
        subnet_id=subnet_id,
        security_group_id=security_group_id,
        cfg=cfg,
        snapshot_tag=snapshot_tag,
    )
    last_stderr = ""
    for attempt in range(1, _VOLCENGINE_MAX_RETRIES_TRANSIENT + 1):
        logger.info(
            "Launching %s type=%s in %s (attempt %d/%d)",
            name, instance_type, zone_id, attempt, _VOLCENGINE_MAX_RETRIES_TRANSIENT,
        )
        rc, stdout, stderr = await _run_ve("ecs", "RunInstances", cfg.region, *args)
        if rc == 0:
            return True, stdout, ""
        last_stderr = stderr

        if _is_zone_capacity_error(stderr):
            logger.warning(
                "type=%s capacity-exhausted in %s: %s",
                instance_type, zone_id, stderr[:300],
            )
            return False, "", last_stderr

        if _is_transient_error(stderr) and attempt < _VOLCENGINE_MAX_RETRIES_TRANSIENT:
            delay = _VOLCENGINE_TRANSIENT_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                "RunInstances transient error (attempt %d/%d): %s — retrying in %ds",
                attempt, _VOLCENGINE_MAX_RETRIES_TRANSIENT, stderr[:200], delay,
            )
            await asyncio.sleep(delay)
            continue

        return False, "", last_stderr
    return False, "", last_stderr


# ============================================================================
# describe / lifecycle helpers
# ============================================================================


def _instance_id_from_run(stdout: str) -> str:
    """RunInstances returns JSON with InstanceIds array."""
    data = json.loads(stdout)
    return data["Result"]["InstanceIds"][0]


async def _describe_instance(
    instance_id: str, cfg: VolcengineProviderConfig,
) -> dict[str, Any] | None:
    """The instance's DescribeInstances record, or None if the call failed / it's gone."""
    rc, stdout, stderr = await _run_ve(
        "ecs", "DescribeInstances", cfg.region, "--InstanceIds.1", instance_id,
    )
    if rc != 0:
        logger.warning("DescribeInstances %s failed: %s", instance_id, stderr[:200])
        return None
    instances = json.loads(stdout)["Result"].get("Instances") or []
    return instances[0] if instances else None


async def _wait_running_with_ip(
    instance_id: str, cfg: VolcengineProviderConfig, *, want_public: bool,
    timeout: float = 300,
) -> str:
    """Poll DescribeInstances until the instance is ``RUNNING`` and has an IP.

    Returns the EIP (want_public) or the primary private IP. Statuses come back
    upper-case (``CREATING`` / ``STARTING`` / ``RUNNING`` / ...)."""
    deadline = time.monotonic() + timeout
    last_state = "?"
    while time.monotonic() < deadline:
        inst = await _describe_instance(instance_id, cfg)
        if inst is not None:
            last_state = str(inst.get("Status") or "?").upper()
            if last_state == "RUNNING":
                if want_public:
                    ip = (inst.get("EipAddress") or {}).get("IpAddress")
                else:
                    nics = inst.get("NetworkInterfaces") or []
                    ip = nics[0].get("PrimaryIpAddress") if nics else None
                if ip:
                    return str(ip)
            elif last_state in ("STOPPED", "ERROR", "DELETING"):
                raise RuntimeError(
                    f"instance {instance_id} entered {last_state} before becoming reachable"
                )
        await asyncio.sleep(5)
    raise RuntimeError(
        f"timed out waiting for {instance_id} to be RUNNING with an IP "
        f"(last state={last_state})"
    )


async def _associate_iam_role(
    instance_id: str, role_name: str, cfg: VolcengineProviderConfig,
) -> None:
    """Attach the instance IAM role that gives the in-box ``tosutil`` TOS access."""
    for attempt in range(1, _VOLCENGINE_MAX_RETRIES_TRANSIENT + 1):
        rc, _, stderr = await _run_ve(
            "ecs", "AssociateInstancesIamRole", cfg.region,
            "--IamRoleName", role_name,
            "--InstanceIds.1", instance_id,
        )
        if rc == 0:
            return
        if not _is_transient_error(stderr) or attempt == _VOLCENGINE_MAX_RETRIES_TRANSIENT:
            raise RuntimeError(
                f"AssociateInstancesIamRole {role_name!r} → {instance_id} failed: {stderr}"
            )
        await asyncio.sleep(_VOLCENGINE_TRANSIENT_BASE_DELAY * (2 ** (attempt - 1)))


async def _delete(instance_id: str, cfg: VolcengineProviderConfig) -> bool:
    """Delete the instance (its EIP + system disk go with it).

    DeleteInstance rejects a still-``CREATING`` instance with
    ``InvalidInstanceStatus``, so that code is retried along with transients."""
    logger.info("Deleting instance %s", instance_id)
    last_stderr = ""
    for attempt in range(_DELETE_MAX_RETRIES):
        rc, _, stderr = await _run_ve(
            "ecs", "DeleteInstance", cfg.region, "--InstanceId", instance_id,
        )
        if rc == 0:
            return True
        last_stderr = stderr
        if "invalidinstancestatus" not in _error_code(stderr) and not _is_transient_error(stderr):
            break
        await asyncio.sleep(_DELETE_RETRY_DELAY)
    logger.error("Failed to delete %s: %s", instance_id, last_stderr)
    return False


async def _stop(instance_id: str, cfg: VolcengineProviderConfig) -> bool:
    logger.info("Stopping instance %s", instance_id)
    rc, _, stderr = await _run_ve(
        "ecs", "StopInstance", cfg.region, "--InstanceId", instance_id,
    )
    if rc != 0:
        logger.error("Failed to stop %s: %s", instance_id, stderr)
        return False
    return True


def _parse_supported_modes(out: str) -> list[tuple[int, int]]:
    """Extract ``(w, h)`` modes from the resolution script's output."""
    marker = "supported="
    i = out.find(marker)
    if i < 0:
        return []
    import ast
    try:
        val = ast.literal_eval(out[i + len(marker):].strip())
        return [(int(a), int(b)) for a, b in val]
    except (ValueError, SyntaxError, TypeError):
        return []


# ============================================================================
# Provider
# ============================================================================


class VolcengineProvider(Provider):
    """Provider backed by ``ve ecs RunInstances / DeleteInstance``."""

    def __init__(self, config: VolcengineProviderConfig | dict[str, Any]):
        if isinstance(config, dict):
            config = _build_provider_config(config)
        self._cfg = config
        self._image_cache: dict[str, str] = {}    # family name → image id
        self._subnet_cache: dict[str, str] = {}   # zone id → subnet id
        self._sg: tuple[str, str] | None = None   # (sg_id, vpc_id), resolved once
        self._iam_role: str | None = None         # effective role name, resolved once

    @property
    def config(self) -> VolcengineProviderConfig:
        return self._cfg

    # ----------------------------------------------------------- resolution

    async def _effective_iam_role(self) -> str:
        """The IAM role to actually attach — resolved + existence-checked once."""
        if self._iam_role is not None:
            return self._iam_role
        name = self._cfg.iam_role_name
        if not name:
            self._iam_role = ""
            return ""
        rc, _, _ = await _run_ve("iam", "GetRole", self._cfg.region, "--RoleName", name)
        if rc == 0:
            self._iam_role = name
        elif self._cfg.output_to_bucket:
            raise RuntimeError(
                f"output_path is a tos:// bucket but IAM role {name!r} does not "
                f"exist — create it so the sandbox can upload output"
            )
        else:
            logger.warning(
                "IAM role %r not found — launching without it (output_path isn't "
                "a bucket, so no in-box TOS access is needed)", name,
            )
            self._iam_role = ""
        return self._iam_role

    async def _resolve_image(self, snap: SnapshotConfig) -> str:
        """Resolve a snapshot's image to a concrete custom-image id."""
        if snap.image_id:
            return snap.image_id
        family = snap.image
        if family in self._image_cache:
            return self._image_cache[family]
        rc, out, err = await _run_ve(
            "ecs", "DescribeImages", self._cfg.region,
            "--Visibility", "private",
            "--Status.1", "available",
            "--TagFilters.1.Key", "ale:image-family",
            "--TagFilters.1.Values.1", family,
            "--MaxResults", "100",
        )
        image_id = ""
        if rc == 0:
            images = json.loads(out)["Result"].get("Images") or []
            if images:
                newest = max(images, key=lambda im: im.get("CreatedAt") or "")
                image_id = str(newest["ImageId"])
        if not image_id:
            raise RuntimeError(
                f"no image tagged ale:image-family={family!r} in {self._cfg.region} "
                f"(import one, tag it, or set an explicit `image_id:` on the snapshot). "
                f"{(err or '').strip()[:200]}"
            )
        self._image_cache[family] = image_id
        return image_id

    async def _resolve_security_group(self) -> tuple[str, str]:
        """Resolve ``config.security_group`` to its id AND the VPC it lives in."""
        if self._sg is not None:
            return self._sg
        ref = self._cfg.security_group
        by = "--SecurityGroupIds.1" if ref.startswith("sg-") else "--SecurityGroupNames.1"
        rc, out, err = await _run_ve(
            "vpc", "DescribeSecurityGroups", self._cfg.region, by, ref,
        )
        sg = None
        if rc == 0:
            groups = json.loads(out)["Result"].get("SecurityGroups") or []
            if len(groups) > 1:
                raise RuntimeError(
                    f"security group name {ref!r} is ambiguous in {self._cfg.region} "
                    f"({[g['SecurityGroupId'] for g in groups]}) — use the id"
                )
            if groups:
                sg = (str(groups[0]["SecurityGroupId"]), str(groups[0]["VpcId"]))
        if sg is None or not sg[0]:
            raise RuntimeError(
                f"security group {ref!r} not found in {self._cfg.region}. "
                f"{(err or '').strip()[:200]}"
            )
        self._sg = sg
        return self._sg

    async def _subnet_for_zone(self, zone_id: str, vpc_id: str) -> str:
        """Resolve a zone id to a Subnet id in that zone within ``vpc_id``."""
        if zone_id in self._subnet_cache:
            return self._subnet_cache[zone_id]
        rc, out, err = await _run_ve(
            "vpc", "DescribeSubnets", self._cfg.region,
            "--VpcId", vpc_id,
            "--ZoneId", zone_id,
            "--PageSize", "100",
        )
        subnet = ""
        if rc == 0:
            subnets = json.loads(out)["Result"].get("Subnets") or []
            tagged = [
                sn for sn in subnets
                if any(
                    t.get("Key") == "project" and t.get("Value") == "ale"
                    for t in sn.get("Tags") or []
                )
            ]
            pick = sorted(tagged or subnets, key=lambda sn: sn.get("SubnetId", ""))
            if pick:
                subnet = str(pick[0]["SubnetId"])
        if not subnet:
            raise RuntimeError(
                f"no subnet in zone {zone_id} (vpc={vpc_id}): {(err or '').strip()[:200]}"
            )
        self._subnet_cache[zone_id] = subnet
        return subnet

    # ------------------------------------------------------------------ acquire

    async def acquire(self, spec: SandboxSpec) -> SandboxHandle:
        snap = self._cfg.snapshots.get(spec.snapshot)
        if snap is None:
            raise KeyError(
                f"snapshot {spec.snapshot!r} not in provider config "
                f"(known: {sorted(self._cfg.snapshots)})"
            )
        if spec.os and spec.os != snap.os:
            logger.warning(
                "os mismatch for %s: task declares %r but image %r looks %r",
                spec.snapshot, spec.os, snap.image, snap.os,
            )

        is_gpu = snap.gpu is not None
        zones = snap.zones

        base_instance = _resolve_instance_type(spec.machine_type, is_gpu=is_gpu)
        instances = _instance_chain(base_instance, is_gpu=is_gpu)

        image_id = await self._resolve_image(snap)
        sg_id, vpc_id = await self._resolve_security_group()
        iam_role = await self._effective_iam_role()

        name = generate_instance_name(
            self._cfg.instance_prefix,
            snapshot=spec.snapshot,
            task_id=spec.task_id,
            harness=spec.harness,
            model_tag=spec.model_tag,
        )

        logger.info(
            "instance %s candidates: image=%s types=%s zones=%s",
            name, image_id, list(instances), list(zones),
        )

        last_stderr = ""
        stdout = ""
        used_zone = zones[0]
        used_subnet = ""
        used_instance = instances[0]

        for instance_type in instances:
            for zone_id in zones:
                try:
                    subnet = await self._subnet_for_zone(zone_id, vpc_id)
                except Exception as e:  # noqa: BLE001
                    last_stderr = f"no subnet for zone {zone_id}: {e}"
                    logger.warning("%s", last_stderr)
                    continue
                ok, out, stderr = await _try_run_in_zone(
                    name=name,
                    image_id=image_id,
                    instance_type=instance_type,
                    zone_id=zone_id,
                    subnet_id=subnet,
                    security_group_id=sg_id,
                    cfg=self._cfg,
                    snapshot_tag=spec.snapshot,
                )
                if ok:
                    stdout = out
                    used_subnet = subnet
                    used_zone = zone_id
                    used_instance = instance_type
                    break
                last_stderr = stderr
                if not _is_zone_capacity_error(stderr):
                    raise RuntimeError(f"volcengine RunInstances failed: {stderr}")
            if stdout:
                break
        else:
            raise RuntimeError(
                f"volcengine RunInstances failed for all types/zones: {last_stderr}"
            )

        try:
            instance_id = _instance_id_from_run(stdout)
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            raise RuntimeError(
                f"failed to parse RunInstances output: {e}\nstdout: {stdout[:500]}"
            ) from e

        try:
            if iam_role:
                await _associate_iam_role(instance_id, iam_role, self._cfg)
            public_ip = await _wait_running_with_ip(
                instance_id, self._cfg,
                want_public=self._cfg.associate_public_ip,
            )

            from ..images import get as get_image
            image = get_image(snap.image)

            cua_url = f"http://{public_ip}:{image.cua_server_port}"
            logger.info(
                "instance %s (%s) launched as %s in %s (%s) at %s",
                name, instance_id, used_instance, used_zone, used_subnet, cua_url,
            )

            cua_timeout = 1200 if snap.os == "windows" else 600
            ready = await wait_cua_ready(cua_url, snap.os, timeout=cua_timeout)
            if not ready:
                raise RuntimeError(f"CUA server at {cua_url} did not become ready")

            if image.os == "windows" and snap.resolution is not None:
                await self._set_windows_resolution(cua_url, snap.resolution)

            return SandboxHandle(
                id=instance_id,
                endpoint=cua_url,
                os=image.os,
                **image.sandbox_paths(),
                metadata={
                    "region": self._cfg.region,
                    "instance_id": instance_id,
                    "instance_type": used_instance,
                    "zone": used_zone,
                    "subnet": used_subnet,
                    "public_ip": public_ip,
                    "image": image.name,
                    "image_id": image_id,
                    "snapshot": spec.snapshot,
                    "name": name,
                },
            )
        except BaseException:
            logger.warning(
                "acquire: post-launch failure on %s (%s) — deleting to avoid leak",
                name, instance_id,
            )
            try:
                await _delete(instance_id, self._cfg)
            except Exception as de:  # noqa: BLE001
                logger.error("acquire: could not delete leaked %s: %s", instance_id, de)
            raise

    @staticmethod
    async def _set_windows_resolution(
        cua_url: str, resolution: tuple[int, int],
    ) -> None:
        """Force the Windows framebuffer to ``resolution`` (w, h)."""
        from cua_bench.computers.remote import RemoteDesktopSession

        w, h = resolution
        remote_path = r"C:\agenthle\_set_resolution.py"
        session = RemoteDesktopSession(api_url=cua_url, os_type="windows")
        _init_computer_skip_wait(session)
        await session.run_command(
            r"cmd /c if not exist C:\agenthle mkdir C:\agenthle", check=False,
        )
        await session.write_file(remote_path, _SET_RES_PY)

        async def _try(tw: int, th: int) -> str:
            res = await session.run_command(f'python "{remote_path}" {tw} {th}', check=False)
            return (res.get("stdout") or "").strip() if isinstance(res, dict) else ""

        out = await _try(w, h)
        if "set_ok" in out:
            logger.info("volcengine: set Windows resolution to %dx%d", w, h)
            return

        modes = _parse_supported_modes(out)
        if modes:
            below = [m for m in modes if m[0] <= w and m[1] <= h]
            best = max(below or modes, key=lambda m: m[0] * m[1])
            out2 = await _try(best[0], best[1])
            if "set_ok" in out2:
                logger.warning(
                    "volcengine: requested Windows resolution %dx%d unsupported "
                    "(supported=%s) — using %dx%d instead",
                    w, h, modes, best[0], best[1],
                )
                return
        err = out or "no output"
        raise RuntimeError(
            f"volcengine: could not set any Windows resolution (requested {w}x{h}); "
            f"adapter output: {err}"
        )

    # ------------------------------------------------------------------ release

    async def release(self, vm: SandboxHandle, *, mode: ReleaseMode = "delete") -> None:
        instance_id = vm.metadata.get("instance_id") or vm.id
        if mode == "delete":
            await _delete(instance_id, self._cfg)
        elif mode == "stop":
            await _stop(instance_id, self._cfg)
        elif mode == "keep":
            logger.info("instance %s kept alive (mode=keep)", instance_id)
        else:
            raise ValueError(f"unknown release mode: {mode!r}")

    # ------------------------------------------------------------------ session

    def open_session(self, vm: SandboxHandle) -> Any:
        from cua_bench.computers.remote import RemoteDesktopSession

        session = RemoteDesktopSession(api_url=vm.endpoint, os_type=vm.os)
        _init_computer_skip_wait(session)
        return session
