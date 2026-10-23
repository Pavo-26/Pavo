#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
if [[ "${1:-}" == "--modern-kfuncs" ]]; then
    cargo build --release --features modern-kfuncs
elif [[ $# == 0 ]]; then
    cargo build --release
else
    printf '%s\n' 'Usage: bash scripts/build.sh [--modern-kfuncs]' >&2
    exit 2
fi
printf '%s\n' 'Built scx_pavo.'
