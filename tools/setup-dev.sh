#!/usr/bin/env bash
# Development dependencies only; never load .env or operate on runtime stores.
set -euo pipefail

browser=false
httpfs=false
for argument in "$@"; do
  case "$argument" in
    --with-browser) browser=true ;;
    --with-httpfs) httpfs=true ;;
    --help|-h)
      echo 'Usage: bash tools/setup-dev.sh [--with-browser] [--with-httpfs]'
      exit 0 ;;
    *) echo "Unknown option: $argument" >&2; exit 2 ;;
  esac
done

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo"
command -v python3 >/dev/null
command -v uv >/dev/null

# Bootstrap Python must provide tomllib (3.11+); project Python is installed below.
read -r required_uv required_python < <(python3 - <<'PY'
import re
import tomllib
from pathlib import Path

config = tomllib.loads(Path("pyproject.toml").read_text())
uv = config["tool"]["uv"]["required-version"]
if not re.fullmatch(r"==\d+\.\d+\.\d+", uv):
    raise SystemExit("Expected an exact [tool.uv].required-version")
print(uv[2:], Path(".python-version").read_text().strip())
PY
)

mkdir -p .cache/dev
{
  printf 'export BAIBAI_DEV_ROOT=%q\n' "$repo/.cache/dev"
  cat <<'ENV'
export UV_CACHE_DIR="$BAIBAI_DEV_ROOT/cache/uv"
export UV_PYTHON_INSTALL_DIR="$BAIBAI_DEV_ROOT/python"
export UV_PYTHON_BIN_DIR="$BAIBAI_DEV_ROOT/bin"
export UV_TOOL_DIR="$BAIBAI_DEV_ROOT/tools"
export UV_TOOL_BIN_DIR="$BAIBAI_DEV_ROOT/bin"
export PLAYWRIGHT_BROWSERS_PATH="$BAIBAI_DEV_ROOT/cache/ms-playwright"
export XDG_CACHE_HOME="$BAIBAI_DEV_ROOT/cache"
export XDG_CONFIG_HOME="$BAIBAI_DEV_ROOT/config"
export PATH="$BAIBAI_DEV_ROOT/bin:$PATH"
ENV
} > .cache/dev/env.sh
source .cache/dev/env.sh
if [[ "$(uv --version | cut -d ' ' -f 2)" != "$required_uv" ]]; then
  uv tool install --no-config --force "uv==$required_uv"
  hash -r
fi
uv python install "$required_python"
project_python=$(uv python find --managed-python --no-project "$required_python")
uv sync --locked --all-groups --python "$project_python"
uv --version
uv run python --version
echo 'Basic setup complete. In each shell, source .cache/dev/env.sh from the repository root.'

result=0
if "$browser"; then
  if ! uv run playwright install chromium; then
    echo 'Chromium install failed; browser provisioning is incomplete.' >&2
    result=1
  fi
fi
if "$httpfs"; then
  if ! uv run python -m tools.diagnostics.provision_duckdb_httpfs; then
    echo 'DuckDB httpfs install failed; remote lake provisioning is incomplete.' >&2
    result=1
  fi
fi
exit "$result"
