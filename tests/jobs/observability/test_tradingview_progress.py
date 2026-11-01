"""進捗の公開境界はSDK未導入でも同じ条件で使える。"""

import json
import os
import subprocess
import sys
from pathlib import Path

from baibai_jobs.observability.tradingview_progress import safe_progress


def test_only_declared_scalar_progress_is_public():
    payload = {
        "phase": "fetch",
        "requests_attempted": 0,
        "symbols_requested": False,
        "chunk_index": None,
        "elapsed_seconds": 1.25,
        "response_bytes": {"private": "nested"},
        "oauth_state": "private-token",
        "response_body": "private-body",
    }
    assert safe_progress(payload) == {
        "phase": "fetch",
        "requests_attempted": 0,
        "symbols_requested": False,
        "chunk_index": None,
        "elapsed_seconds": 1.25,
    }
    assert payload["oauth_state"] == "private-token"


def test_progress_reader_imports_without_site_packages_or_engine(tmp_path):
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            (
                "import json, sys; "
                "from baibai_jobs.observability.tradingview_progress import safe_progress; "
                "assert not any(name.startswith(('baibai_engine', 'httpx', 'mcp')) "
                "for name in sys.modules); "
                "print(json.dumps(safe_progress({'requests_attempted': 0, 'token': 'private'})))"
            ),
        ],
        env={**os.environ, "PYTHONPATH": str(root / "jobs/src")},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {"requests_attempted": 0}
    assert "private" not in result.stdout + result.stderr
