#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
"${repo_root}/batch/scripts/r2_transfer.sh" seed-all
printf 'initial market/runs/macro/baibai store seed uploaded\n'
