"""One-shot SSD envelope for the reviewed, credential-free #2963 Machine fixture."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

HEAD = "2490e9fa31a9a620567d74fb4cf4a2574441e95d"
TREE = "485efb5cbac2b0717bfb2c82d5bac4c54d6dfa52"
MACHINE_START_B = 14_000_000_000
STOP_B = 2_000_000_000
SINGLE_PUT_B = 5_363_466_240
# Previously observed size inputs, not a constant production provisioning contract.
HYDRATED_MARKET_B = 6_495_543_296
INCUMBENT_RUNS_B = 4_386_460_672
DAILY_SEAL_LOWER_BOUND_B = HYDRATED_MARKET_B * 3 + INCUMBENT_RUNS_B
PHASES = {
    "sqlite_fixture_padding",
    "real_compact_snapshot",
    "real_adapter_upload",
    "real_adapter_download_and_readback",
}
MEASURED = "MACHINE_FIXTURE_MEASURED"
ADMISSION_ONLY = "DAILY_SEAL_LOWER_BOUND_AVAILABLE_ONLY"


def measure(paths: dict[str, Path]) -> dict[str, dict[str, int]]:
    result = {}
    for label, path in paths.items():
        info = os.statvfs(path)
        result[label] = {
            "device": path.stat().st_dev,
            "total_bytes": info.f_blocks * info.f_frsize,
            "available_bytes": info.f_bavail * info.f_frsize,
            "available_inodes": info.f_favail,
        }
    return result


def check_fixture_evidence(result: dict) -> None:
    sealed, multipart = result["sealed_runs"], result["multipart"]
    size = sealed["bytes"]
    conditions = {
        "exact head and tree": (result["basis_commit"], result["verified_tree"]) == (HEAD, TREE),
        "real over-limit allocation": size > SINGLE_PUT_B and sealed["allocated"] == size,
        "full fixture": result["status"] == "pass" and result["sqlite_fixture_padding_rows"] == 5240,
        "complete wire evidence": (
            multipart["observed"] is True
            and multipart["part_calls"] > 1
            and multipart["part_bytes"] == size
            and multipart["complete_calls"] == 1
        ),
        "independent byte readback": result["roundtrip_content"]["same_sha256_in_independent_files"] is True,
        "on-disk per-phase minima": (
            result["filesystem"]["same_device"] is True
            and result["filesystem"]["not_tmpfs"] is True
            and PHASES <= result["filesystem"]["phases"].keys()
        ),
    }
    missing = [name for name, passed in conditions.items() if not passed]
    if missing:
        raise ValueError("missing full-size evidence: " + ", ".join(missing))


def run_fixture(checkout: Path, fixture: Path, watch: dict[str, Path], minima: dict[str, int]) -> dict:
    if minima["stores"] < MACHINE_START_B:
        raise RuntimeError(f"INSUFFICIENT_START_FREE: {minima['stores']} < {MACHINE_START_B} B")
    with tempfile.TemporaryDirectory(prefix=".runner-pair-", dir=watch["stores"]) as directory:
        work = Path(directory)
        home, tmp = work / "home", work / "tmp"
        home.mkdir()
        tmp.mkdir()
        shutil.copyfile(fixture, work / "probe.py")
        (work / "exact-tree.txt").write_text(TREE + "\n", encoding="utf-8")
        env = {
            name: os.environ[name]
            for name in ("PATH", "LD_LIBRARY_PATH", "LANG", "LC_ALL")
            if name in os.environ
        } | {
            "HOME": str(home),
            "TMPDIR": str(tmp),
            "BAIBAI_PAIR_REPO": str(checkout),
            "BAIBAI_PAIR_WORKDIR": str(work),
            "PYTHONPATH": os.pathsep.join(str(checkout / path) for path in ("engine/src", "jobs/src", ".")),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        deadline = time.monotonic() + 3300

        def signal_stop(signum: int, _frame: object) -> None:
            raise KeyboardInterrupt(f"interrupted by signal {signum}")

        previous_term = signal.signal(signal.SIGTERM, signal_stop)
        with (work / "stdout.json").open("w", encoding="utf-8") as stdout:
            child = subprocess.Popen(
                [str(checkout / ".venv/bin/python"), str(work / "probe.py")],
                cwd=checkout,
                env=env,
                stdout=stdout,
                start_new_session=True,
            )
            try:
                while True:
                    status = child.poll()
                    for label, observed in measure(watch).items():
                        minima[label] = min(minima[label], observed["available_bytes"])
                    if minima["stores"] < STOP_B:
                        raise RuntimeError(f"LOW_FREE_STOP: {minima['stores']} < {STOP_B} B")
                    if status is not None:
                        if status:
                            raise RuntimeError(f"FIXTURE_FAILED: child exited {status}")
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError("FIXTURE_TIMEOUT: exceeded 3300 seconds")
                    time.sleep(0.25)
            finally:
                if child.poll() is None:
                    try:
                        # Only this disposable test's process group.
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait()
                signal.signal(signal.SIGTERM, previous_term)
        result = json.loads((work / "sanitized-summary.json").read_text(encoding="utf-8"))
        check_fixture_evidence(result)
        minima["stores"] = min(minima["stores"], result["filesystem"]["free_min_sampled"])
        if minima["stores"] < STOP_B:
            raise RuntimeError(f"LOW_FREE_STOP: {minima['stores']} < {STOP_B} B")
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True, type=Path)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--fixture", type=Path)
    modes.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--runner-temp", required=True, type=Path)
    args = parser.parse_args()
    checkout = args.checkout.resolve(strict=True)
    watch = {"stores": checkout / "stores", "runner_temp": args.runner_temp, "system_tmp": Path("/tmp")}
    initialized = measure(watch)
    minima = {label: info["available_bytes"] for label, info in initialized.items()}
    initial_free = minima["stores"]
    daily_bound = "HAVING_ROOM_FOR_LOWER_BOUND_ONLY" if initial_free >= DAILY_SEAL_LOWER_BOUND_B else "LOWER_BOUND_UNMET"
    print(json.dumps({"baseline": initialized, "daily_bound": daily_bound,
                      "daily_seal_lower_bound_bytes": DAILY_SEAL_LOWER_BOUND_B}), flush=True)
    status, reason = MEASURED, ""
    try:
        for ref, expected in (("HEAD", HEAD), ("HEAD^{tree}", TREE)):
            observed = subprocess.check_output(["git", "rev-parse", ref], cwd=checkout, text=True).strip()
            if observed != expected:
                raise ValueError(f"expected {ref} {expected}; observed {observed}")
        # Never claim an SSD measurement if a runner puts the fixture on RAM or a staging overlay.
        fstype = subprocess.check_output(["findmnt", "-n", "-o", "FSTYPE", "-T", watch["stores"]], text=True).strip()
        if fstype not in ("ext4", "xfs", "btrfs", "ext2", "ext3"):
            raise ValueError(f"not a recognized runner disk mount: {fstype}")
        if args.preflight_only:
            if initial_free < DAILY_SEAL_LOWER_BOUND_B:
                raise ValueError(
                    f"historical-size seal admission needs {DAILY_SEAL_LOWER_BOUND_B} B "
                    f"before other disk use; observed {initial_free} B"
                )
            status = ADMISSION_ONLY
        else:
            print(json.dumps({"fixture": run_fixture(checkout, args.fixture, watch, minima)}), flush=True)
    except (RuntimeError, ValueError, OSError, KeyError, subprocess.CalledProcessError, KeyboardInterrupt) as exc:
        status = "DAILY_SEAL_LOWER_BOUND_UNMET_OR_UNMEASURED" if args.preflight_only else "MACHINE_FIXTURE_NOT_MEASURED"
        reason = str(exc)
    result = {"status": status, "reason": reason, "minimum_available_bytes": minima,
              "after_owned_fixture_cleanup": measure(watch),
              "machine_fixture": "MEASURED" if status == MEASURED else "NOT_ACCEPTED",
              "daily_hydrate_snapshot": "NOT_RUN", "real_r2": "NOT_RUN", "daily_bound": daily_bound}
    print(json.dumps(result), flush=True)
    if destination := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(destination).open("a", encoding="utf-8") as summary:
            summary.write(f"## #2963: {status}\n\n{reason}\n\n")
            summary.write("Normal daily hydrate/snapshot: **NOT RUN**. Real R2: **NOT RUN**.\n\n")
            summary.write(f"Historical-size daily seal lower bound: **{daily_bound}** "
                          f"({initial_free:,} B available vs {DAILY_SEAL_LOWER_BOUND_B:,} B before other data).\n\n")
            summary.write("| Watch path | Device | Initial available B | Minimum available B |\n")
            summary.write("|---|---:|---:|---:|\n")
            for label, info in initialized.items():
                summary.write(f"| {label} | {info['device']} | {info['available_bytes']} | {minima[label]} |\n")
    return 0 if status in (MEASURED, ADMISSION_ONLY) else 1


if __name__ == "__main__":
    raise SystemExit(main())
