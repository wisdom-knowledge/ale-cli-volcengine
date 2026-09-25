#!/usr/bin/env bash
# Import ALE sandbox images from gs://ale-data-public/images/ into Volcengine as
# ECS custom images. Mirror of scripts/aliyun/import_images.sh. Idempotent-ish:
# skips the TOS upload if the raw is already there.
#
#   ./import_images.sh ale-ubuntu22       # Linux: ImportImage, bake tosutil
#   ./import_images.sh ale-win10          # Windows 10 desktop
#   ./import_images.sh ale-win-server     # Windows Server (GPU)
#   ./import_images.sh all                # all three
#
# Per image it: stages the GCS tar.gz (disk.raw) locally, uploads it to the TOS
# import bucket, turns it into a custom image via `ve ecs ImportImage`, and tags
# it `ale:image-family=<name>` (the tag VolcengineProvider resolves). For Linux
# it then bakes `tosutil` into the image (needed for tos:// data / output — the
# provider's in-box wrapper fetches the instance role's STS creds itself, so only
# the binary is needed) by launching the imported image, installing through the
# in-guest cua server, and re-capturing with CreateImage.
#
# Prerequisites:
#   • TOS + ECS activated on the account.
#   • ambient `ve` credentials (`ve configure` / `ve login`); `ve >= 1.1.11`.
#   • host gcloud login + a billing project for requester-pays GCS reads.
#   • a security group `ale-sandbox` with a subnet in its VPC, allowing inbound
#     tcp/5000 (cua) from this host.
set -euo pipefail
export VE_CALLER_TYPE="${VE_CALLER_TYPE:-cli}"
say() { printf '\n=== %s ===\n' "$*"; }
J()  { python3 -c "import sys,json;print(json.load(sys.stdin)$1)"; }
R="${ALE_VOLC_REGION:-cn-beijing}"
Z="${ALE_VOLC_ZONE:-$R-a}"
ACCT=$(ve sts GetCallerIdentity | J "['Result']['AccountId']")
BUCKET="ale-image-import-$ACCT"
GCS=gs://ale-data-public/images
BILLING="${ALE_GCS_BILLING_PROJECT:-agenthle-488519}"   # requester-pays GCS reads
SG_NAME=ale-sandbox
BAKE_TYPE="${ALE_BAKE_INSTANCE_TYPE:-ecs.g4i.2xlarge}"
TOSUTIL_URL="${ALE_TOSUTIL_URL:-https://tos-tools.tos-cn-beijing.volces.com/linux/amd64/tosutil}"

# Native OpenAPI call: `ve <svc> <Action> <PascalCase params...>` in region $R.
# --force/--version/--endpoint match VolcengineProvider._run_ve.
ecs() { ve ecs "$@" --region "$R" --version 2020-04-01 --endpoint open.volcengineapi.com --force --output json; }
vpc() { ve vpc "$@" --region "$R" --version 2020-04-01 --endpoint open.volcengineapi.com --force --output json; }

ensure_bucket() {                    # TOS import bucket exists (region-local)
  ve tosutil ls "tos://$BUCKET" -limit=1 >/dev/null 2>&1 \
    || ve tosutil mb "tos://$BUCKET" -re="$R" >/dev/null
}

ensure_raw() {                       # $1 family → ensure tos://$BUCKET/images/$1.raw
  local key="images/$1.raw"
  if ve tosutil stat "tos://$BUCKET/$key" >/dev/null 2>&1; then echo "raw present: $1"; return; fi
  # tosutil cp stat()s its source, so GCS→tar→TOS can't be piped. Stage the
  # decompressed raw locally (needs ~170 GiB free), upload, then delete.
  # ALE_IMG_SCRATCH overrides the scratch location (default /var/tmp/volcimg).
  local scratch="${ALE_IMG_SCRATCH:-/var/tmp/volcimg}" raw
  mkdir -p "$scratch"; raw="$scratch/$1.raw"
  say "stage $GCS/$1.tar.gz -> $raw (decompress)"
  gsutil -u "$BILLING" cat "$GCS/$1.tar.gz" | tar -xzO > "$raw"
  [ -s "$raw" ] || { echo "staging produced an empty raw for $1" >&2; return 1; }
  say "upload $raw -> tos://$BUCKET/$key"
  ve tosutil cp "$raw" "tos://$BUCKET/$key" -f
  rm -f "$raw"
}

sg_id() { vpc DescribeSecurityGroups --SecurityGroupNames.1 "$SG_NAME" \
    | J "['Result']['SecurityGroups'][0]['SecurityGroupId']"; }

subnet_in_sg_vpc() {                 # echo a subnet id in the SG's VPC, in zone $Z
  local v; v=$(vpc DescribeSecurityGroups --SecurityGroupNames.1 "$SG_NAME" \
    | J "['Result']['SecurityGroups'][0]['VpcId']")
  vpc DescribeSubnets --VpcId "$v" --ZoneId "$Z" | J "['Result']['Subnets'][0]['SubnetId']"
}

wait_cua() {                         # $1 ip — wait up to 20 min for cua :5000
  local ip=$1
  for _ in $(seq 1 80); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 "http://$ip:5000/status" 2>/dev/null)" = 200 ] \
      && { echo "cua ready at $ip"; return 0; }
    sleep 15
  done
  echo "cua never came up at $ip"; return 1
}

poll_image() {                       # $1 image-id → wait until available
  while :; do
    local st; st=$(ecs DescribeImages --ImageIds.1 "$1" \
      | J "['Result']['Images'][0]['Status'].lower()" 2>/dev/null || echo '?')
    echo "  $1 $st" >&2
    [ "$st" = available ] && return
    case "$st" in error|failed) echo "FAILED" >&2; return 1;; esac
    sleep 60
  done
}

tag_image() {                        # $1 image-id, $2 family
  ecs CreateTags --ResourceType image --ResourceIds.1 "$1" \
    --Tags.1.Key ale:image-family --Tags.1.Value "$2" \
    --Tags.2.Key Name --Tags.2.Value "$2" >/dev/null
  echo "tagged $1 (ale:image-family=$2)"
}

delete_instance() { ecs DeleteInstance --InstanceId "$1" >/dev/null 2>&1 || true; }

import_base() {                      # $1 family, $2 OsType, $3 Platform → echo ImageId
  local fam=$1 ostype=$2 platform=$3 img
  ensure_bucket >&2; ensure_raw "$fam" >&2
  say "$fam: ImportImage (OsType=$ostype Platform=$platform)" >&2
  # Explicit OsType/Platform so a Windows disk isn't mis-detected as Linux
  # (the Alibaba import skips Windows driver injection in that case; assume the
  # same failure mode here).
  img=$(ecs ImportImage --ImageName "$fam-base-$(date +%s)" \
    --OsType "$ostype" --Platform "$platform" --Architecture amd64 --BootMode UEFI \
    --Url "https://$BUCKET.tos-$R.volces.com/images/$fam.raw" \
    | J "['Result']['ImageId']")
  poll_image "$img" >&2 || { echo FAILED; return 1; }
  echo "$img"
}

import_linux() {                     # $1 family — import, bake tosutil
  local fam=$1 base sg subnet iid ip='' final
  base=$(import_base "$fam" Linux Ubuntu); [ "$base" = FAILED ] && return 1
  say "$fam: bake tosutil (launch base, install via cua, CreateImage)"
  sg=$(sg_id); subnet=$(subnet_in_sg_vpc)
  iid=$(ecs RunInstances --ImageId "$base" --InstanceTypeId "$BAKE_TYPE" --ZoneId "$Z" \
    --InstanceName ale-rebake --InstanceChargeType PostPaid --KeepImageCredential true \
    --NetworkInterfaces.1.SubnetId "$subnet" --NetworkInterfaces.1.SecurityGroupIds.1 "$sg" \
    --Volumes.1.VolumeType ESSD_PL0 --Volumes.1.Size 100 --Volumes.1.DeleteWithInstance true \
    --EipAddress.BandwidthMbps 100 --EipAddress.ChargeType PayByTraffic \
    --EipAddress.ReleaseWithInstance true \
    --Tags.1.Key purpose --Tags.1.Value ale-run | J "['Result']['InstanceIds'][0]")
  for _ in $(seq 1 40); do
    ip=$(ecs DescribeInstances --InstanceIds.1 "$iid" \
      | J "['Result']['Instances'][0].get('EipAddress',{}).get('IpAddress','')" 2>/dev/null || true)
    [ -n "$ip" ] && break; sleep 10
  done
  wait_cua "$ip" || { delete_instance "$iid"; return 1; }
  # Install tosutil inside the guest, driven through the cua server. 900s: cold install.
  local inst="curl -fsSL $TOSUTIL_URL -o /tmp/tosutil && sudo install -m755 /tmp/tosutil /usr/local/bin/tosutil && /usr/local/bin/tosutil version"
  curl -s --max-time 900 -X POST "http://$ip:5000/cmd" -H 'Content-Type: application/json' \
    -d "$(python3 -c 'import json,sys;print(json.dumps({"command":"run_command","params":{"command":sys.argv[1]}}))' "$inst")" \
    | tr '\r' '\n' | grep '^data:' | head -1 | sed 's/^data: //' | J "['stdout'][-60:]" || true
  final=$(ecs CreateImage --InstanceId "$iid" --ImageName "$fam-$(date +%s)" \
    --Description "$fam + tosutil" | J "['Result']['ImageId']")
  # Under `set -e`, a poll_image failure would exit before the cleanup below —
  # leaking the (billable) builder instance. Delete it on failure.
  poll_image "$final" || { delete_instance "$iid"; return 1; }
  tag_image "$final" "$fam"
  # retire base image + builder
  ecs DeleteImages --ImageIds.1 "$base" --DeleteBindedSnapshots true >/dev/null 2>&1 || true
  delete_instance "$iid"
  echo "LINUX_DONE $fam -> $final"
}

import_windows() {                   # $1 family — import + tag (no bake needed)
  local fam=$1 img
  img=$(import_base "$fam" Windows "Windows Server"); [ "$img" = FAILED ] && return 1
  tag_image "$img" "$fam"
  echo "WINDOWS_DONE $fam -> $img"
}

case "${1:?usage: import_images.sh ale-ubuntu22|ale-win10|ale-win-server|all}" in
  ale-ubuntu22)   import_linux ale-ubuntu22 ;;
  ale-win10)      import_windows ale-win10 ;;
  ale-win-server) import_windows ale-win-server ;;
  all)            import_linux ale-ubuntu22; import_windows ale-win10; import_windows ale-win-server ;;
  *) echo "unknown image $1"; exit 2 ;;
esac
