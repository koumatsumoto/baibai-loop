#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
"${repo_root}/tools/cloud/r2_transfer.sh" push-all
printf 'initial market/runs/macro/baibai store seed uploaded\n'
