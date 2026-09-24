#!/usr/bin/env bash
# Sync ALE task data from gs://ale-data-public (GCS) to the Volcengine TOS
# task-data bucket. Mirror of scripts/aliyun/sync_task_data.sh.
#
# gsutil has no TOS backend and tosutil can't read gs://, so this stages to a
# local temp dir then `ve tosutil cp -r` into TOS. To keep local disk bounded on
# the full dataset (~260 GiB across domains), the default whole-bucket sync runs
# ONE top-level domain at a time (stage → push → wipe → next). images/ is
# excluded (VM image exports, not task data — see import_images.sh).
#
# Auth: GCS reads use the host's gcloud login + a billing project (ale-data-public
# is requester-pays); TOS writes use the ambient `ve` credentials.
#
# Usage:
#   ./sync_task_data.sh                              # gs://ale-data-public -> tos://ale-data-<acct>, all domains
#   ./sync_task_data.sh gs://src-subtree tos://dst   # one subtree (staged whole)
set -euo pipefail
export VE_CALLER_TYPE="${VE_CALLER_TYPE:-cli}"
J() { python3 -c "import sys,json;print(json.load(sys.stdin)$1)"; }
ACCT=$(ve sts GetCallerIdentity | J "['Result']['AccountId']")
SRC="${1:-gs://ale-data-public}"
DST="${2:-tos://ale-data-$ACCT}"
BILLING="${ALE_GCS_BILLING_PROJECT:-agenthle-488519}"   # billed for requester-pays GCS reads
STAGE_ROOT="${ALE_STAGE_DIR:-$(mktemp -d)}"
trap 'rm -rf "$STAGE_ROOT"' EXIT

case "$SRC" in gs://*) ;; *) echo "src must be gs://..."; exit 2;; esac
case "$DST" in tos://*) ;; *) echo "dst must be tos://..."; exit 2;; esac

stage_push() {                       # $1 gs-subtree  $2 tos-subtree
  local stage; stage=$(mktemp -d "$STAGE_ROOT/XXXX")
  echo "  stage $1 -> $stage"
  gsutil -u "$BILLING" -m rsync -r "$1" "$stage"
  echo "  push  $stage -> $2"
  # -flat: upload the stage dir's CONTENTS, not the dir itself.
  ve tosutil cp "$stage" "$2/" -r -f -flat
  rm -rf "$stage"
}

if [ "$SRC" = "gs://ale-data-public" ] && [ -z "${2:-}" ]; then
  echo "syncing all domains $SRC -> $DST (GCS billed to $BILLING; excluding images/)"
  for d in $(gsutil -u "$BILLING" ls "$SRC/" | sed -n 's#.*/\([^/]*\)/$#\1#p'); do
    [ "$d" = images ] && { echo "skip images/"; continue; }
    echo "=== domain: $d ==="
    stage_push "$SRC/$d" "$DST/$d"
  done
  echo "DONE: all domains synced to $DST"
else
  echo "syncing $SRC -> $DST (GCS billed to $BILLING)"
  stage_push "$SRC" "$DST"
  echo "DONE: $DST is in sync with $SRC"
fi
