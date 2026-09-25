"""Offline unit tests for the Volcengine provider + TOS task-data wiring.

No cloud calls: `ve` subprocesses are stubbed, and the yaml profile is loaded
through the real config loader.
"""
from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from ale_run.environments import task_data
from ale_run.environments.providers import volcengine as ve_mod
from ale_run.environments.task_data import tosbucket
from ale_run.orchestration.config_loader import _build_environment_from_path
from ale_run.orchestration.factory import build_provider

REPO = Path(__file__).resolve().parents[2]
PROFILE = REPO / "configs/environments/environment_volcengine.yaml"


def _cfg(**over):
    raw = {
        "region": "cn-beijing",
        "snapshots": {"cpu-free-ubuntu": {"image": "ale-ubuntu22", "zones": ["cn-beijing-a"]}},
        **over,
    }
    return ve_mod._build_provider_config(raw)


def _run_args(cfg):
    return ve_mod._build_run_args(
        name="ale-x", image_id="image-1", instance_type="ecs.g4i.2xlarge",
        zone_id="cn-beijing-a", subnet_id="subnet-1", security_group_id="sg-1",
        cfg=cfg, snapshot_tag="cpu-free-ubuntu",
    )


def _pairs(args):
    return dict(zip(args[::2], args[1::2]))


# ---- config ----


def test_provider_config_defaults():
    cfg = _cfg()
    assert cfg.security_group == "ale-sandbox"
    assert cfg.system_disk_category == "ESSD_PL0"
    assert cfg.system_disk_size == 100
    assert cfg.instance_charge_type == "PostPaid"
    assert cfg.associate_public_ip
    snap = cfg.snapshots["cpu-free-ubuntu"]
    assert snap.os == "linux" and snap.zones == ("cn-beijing-a",)


def test_snapshot_requires_zones():
    with pytest.raises(KeyError, match="zones"):
        _cfg(snapshots={"s": {"image": "ale-win10"}})


def test_profile_loads_through_config_loader():
    env, artifacts = _build_environment_from_path(str(PROFILE), base_dir=REPO)
    spec = env.provider_specs["volcengine"]
    assert set(spec.config["snapshots"]) == {"cpu-free-ubuntu", "cpu-free", "cpu-license"}
    assert spec.config["region"] == "cn-beijing"
    assert spec.config["iam_role_name"] == "ale-sandbox"
    assert env.snapshot_kind["cpu-free"] == "volcengine"
    provider = build_provider(spec)
    assert isinstance(provider, ve_mod.VolcengineProvider)
    assert provider.config.snapshots["cpu-free"].resolution == (1024, 768)
    assert artifacts.task_data_source == "baked_in_sandbox"


def test_tos_output_flags_bucket_output(tmp_path):
    text = PROFILE.read_text().replace("output_path: null", "output_path: tos://ale-out")
    p = tmp_path / "env.yaml"
    p.write_text(text)
    env, artifacts = _build_environment_from_path(str(p), base_dir=tmp_path)
    assert artifacts.output_path == "tos://ale-out"
    assert env.provider_specs["volcengine"].config["output_to_bucket"] is True


# ---- RunInstances argv ----


def test_run_args_public_ip_and_image_credential():
    args = _run_args(_cfg())
    kv = _pairs(args)
    assert kv["--InstanceTypeId"] == "ecs.g4i.2xlarge"
    assert kv["--NetworkInterfaces.1.SubnetId"] == "subnet-1"
    assert kv["--NetworkInterfaces.1.SecurityGroupIds.1"] == "sg-1"
    assert kv["--Volumes.1.Size"] == "100"
    assert kv["--EipAddress.ChargeType"] == "PayByTraffic"
    assert kv["--EipAddress.ReleaseWithInstance"] == "true"
    assert kv["--KeepImageCredential"] == "true"
    assert "--KeyPairName" not in kv


def test_run_args_private_with_key_pair():
    kv = _pairs(_run_args(_cfg(internet_max_bandwidth=0, key_name="kp")))
    assert not any(k.startswith("--EipAddress") for k in kv)
    assert kv["--KeyPairName"] == "kp"
    assert "--KeepImageCredential" not in kv


# ---- error classification ----


@pytest.mark.parametrize("stderr, transient, zone", [
    ("InvalidAccessKey: The access key is invalid", False, False),
    ("Throttling: Request was denied due to flow control", True, False),
    ("InternalError: oops", True, False),
    ("InsufficientInventory: no stock in zone", False, True),
    ("InvalidInstanceType.NotFound: type not in zone", False, True),
    ("dial tcp: i/o timeout", True, False),
])
def test_error_classification(stderr, transient, zone):
    assert ve_mod._is_transient_error(stderr) is transient
    assert ve_mod._is_zone_capacity_error(stderr) is zone


def test_error_code_parses_leading_token():
    assert ve_mod._error_code("InvalidInstanceStatus: busy") == "invalidinstancestatus"
    assert ve_mod._error_code("no code here") == ""


# ---- ve argv contract ----


async def test_run_ve_appends_force_version_endpoint(monkeypatch):
    seen = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b'{"Result": {}}', b""

    async def fake_exec(*cmd, **_):
        seen["cmd"] = list(cmd)
        return _Proc()

    monkeypatch.setattr(ve_mod.asyncio, "create_subprocess_exec", fake_exec)
    rc, out, _ = await ve_mod._run_ve("vpc", "DescribeSubnets", "cn-shanghai", "--VpcId", "v")
    assert rc == 0 and out == '{"Result": {}}'
    cmd = seen["cmd"]
    assert cmd[:5] == ["ve", "vpc", "DescribeSubnets", "--VpcId", "v"]
    kv = _pairs(cmd[5:9])
    assert kv["--region"] == "cn-shanghai"
    assert kv["--version"] == "2020-04-01"
    assert "--force" in cmd and cmd[-2:] == ["--output", "json"]


# ---- TOS task data ----


def test_tos_source_dispatch():
    assert task_data.select("tos://ale-data") is tosbucket


def test_tos_wrapper_is_valid_python():
    compile(tosbucket._TOS_WRAPPER_PY, "_tos_wrapper.py", "exec")


async def test_tosutil_requires_region():
    class _Box:
        metadata: ClassVar[dict] = {}

    with pytest.raises(RuntimeError, match="region"):
        await tosbucket.tosutil(_Box(), "ls", "tos://b/", timeout=5)
