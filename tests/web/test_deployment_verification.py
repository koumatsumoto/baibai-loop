"""配信確認scriptは合成credentialとfake curlで全routeを確認する。"""

import json
import os
import pty
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("extra_route", [False, True])
def test_deployment_verifier_uses_the_route_contract_from_any_directory(tmp_path, extra_route):
    root = tmp_path / "web"
    scripts = root / "edge/scripts"
    scripts.mkdir(parents=True)
    script = scripts / "verify-deployment.sh"
    shutil.copy2(ROOT / "web/edge/scripts/verify-deployment.sh", script)
    contracts = root / "contracts"
    contracts.mkdir()
    contract = json.loads((ROOT / "web/contracts/routes.json").read_text())
    if extra_route:
        contract["routes"]["/api/synthetic-added-view"] = "views/synthetic.json"
    (contracts / "routes.json").write_text(json.dumps(contract))
    routes = [*contract["computed"], *contract["routes"]]
    binary = tmp_path / "bin"
    binary.mkdir()
    curl = binary / "curl"
    curl.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit
args = sys.argv[1:]
url = urlsplit(args[-1])
header = sys.stdin.read() if '--header' in args else ''
if url.scheme == 'http':
    status = 308
elif header != 'Authorization: Bearer synthetic-valid\\n':
    status = 401
else:
    status = 200 if url.path in json.loads(os.environ['VERIFIED_ROUTES']) else 404
if '--dump-header' in args:
    Path(args[args.index('--dump-header') + 1]).write_text(
        'Cache-Control: no-store\\nStrict-Transport-Security: max-age=31536000\\n'
    )
with open(os.environ['VERIFY_LOG'], 'a') as log:
    log.write(json.dumps([url.path, status]) + '\\n')
print(status, end='')
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    log = tmp_path / "requests.jsonl"
    master, slave = pty.openpty()
    try:
        os.write(master, b"synthetic-valid\n")
        result = subprocess.run(
            [str(script)],
            cwd=tmp_path,
            stdin=slave,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env={
                **os.environ,
                "PATH": f"{binary}{os.pathsep}{os.environ['PATH']}",
                "WORKER_BASE_URL": "https://synthetic.invalid",
                "VERIFIED_ROUTES": json.dumps(routes),
                "VERIFY_LOG": str(log),
            },
        )
    finally:
        os.close(master)
        os.close(slave)
    assert result.returncode == 0, result.stderr
    requests = [json.loads(line) for line in log.read_text().splitlines()]
    for route in routes:
        assert [status for path, status in requests if path == route] == [401, 401, 200]
    assert ["/", 308] in requests
    assert "synthetic-valid" not in result.stdout + result.stderr
