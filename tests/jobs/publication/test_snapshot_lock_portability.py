"""Reading a GC plan does not require the optional POSIX writer lock."""

import json
import subprocess
import sys


def test_planning_without_fcntl_succeeds_but_apply_never_enters_the_writer(tmp_path):
    # Fresh processes exercise the real Jobs CLI import path on either host OS.
    command = [
        sys.executable,
        "-c",
        (
            "import sys; sys.modules['fcntl'] = None; "
            "from baibai_jobs.cli import main; raise SystemExit(main())"
        ),
        "snapshots",
        "gc",
        "--mirror",
        str(tmp_path / "mirror"),
    ]
    plan = subprocess.run(command, capture_output=True, text=True, check=True)
    digest = json.loads(plan.stdout)["plan_hash"]
    assert not (tmp_path / "mirror").exists()

    apply = subprocess.run(
        [*command, "--apply", "--plan-hash", digest], capture_output=True, text=True, check=False
    )
    assert apply.returncode == 1
    assert "requires POSIX file locking" in apply.stderr
    assert not (tmp_path / "mirror").exists()
