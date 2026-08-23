"""Build and evaluate the versioned long-horizon calibration cache."""

from __future__ import annotations

import sys
import uuid
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date
from os import link
from pathlib import Path
from shutil import copytree, rmtree
from typing import TextIO, cast

import yaml

from baibai_engine.foundation.filesystem import write_text_atomic
from baibai_engine.market.lake.identity import verified_git_commit
from baibai_engine.market.lake.models import (
    CohortSourceRef,
    L1ReleaseSourceRef,
    source_assurance,
)
from baibai_engine.market.lake.objects import LakeObjectError, open_lake
from baibai_engine.market.lake.reader import LakeReadError, release_backing_store
from baibai_engine.market.lake.retention import lake_writer_lock
from baibai_engine.market.lake.writer import (
    LakeBuildError,
    LegacySQLiteSnapshot,
    sealed_sqlite_snapshot,
)
from baibai_engine.market.sqlite.lake_origin import LakeStoreOriginError, read_lake_store_origin

from ..estimates import EXPECTED_RETURN_MODEL_VERSION
from ..rule_config import ScreeningRules
from ..store_readiness import unreadable_store_reason
from .authority import (
    KNOWN_METRICS,
    PRODUCTION_REQUIRED_METRICS,
    CohortIntegrity,
    EvaluationScope,
    decide_authority,
)
from .context import CalibrationContextError, build_er_distribution_context
from .evaluation import OPTIONAL_SENSITIVITY_METRICS, evaluate_cohorts
from .forward import (
    CONTROL_EVENT_EXIT_STATUS,
    FAILURE_EXIT_STATUS,
    HORIZONS,
    ForwardObservationPolicy,
    ForwardReturnRow,
    compute_forward_returns,
    latest_market_data_date,
    read_control_event_exits,
    read_failure_exits,
)
from .grid import month_end_asof_grid
from .lake import CalibrationBundleRef, CalibrationLakeError
from .panel import (
    PANEL_BUILD_POLICIES,
    PRODUCTION_PANEL_POLICY,
    CalibrationError,
    PanelRow,
    PanelVariant,
    build_panel,
    rules_content_hash,
)
from .store import (
    CACHE_SCHEMA_VERSION,
    DEFAULT_CALIBRATION_DIR,
    CalibrationCacheError,
    adopt_bundle_generation,
    current_bundle_ref,
    has_cohort,
    published_cohorts,
    read_forward,
    read_panel,
    read_panel_meta,
    resolve_calibration_bundle,
    write_forward,
    write_panel,
)


def _market_mirror(sqlite_path: Path) -> Path | None:
    """Where the L1 releases this store was filled from live.

    The lake mirror is the directory the stores live in, so the market store's
    grandparent is it. Absent when that directory holds no lake at all, which is the
    shape of every fixture that builds a calibration store on its own.
    """

    mirror = sqlite_path.resolve().parent.parent
    return mirror if (mirror / "lake").is_dir() else None


def _l1_release_source(
    sqlite_path: Path,
    *,
    mirror: Path | None,
    asserted_release_id: str | None,
    asserted_manifest_sha256: str | None,
) -> L1ReleaseSourceRef | None:
    """The L1 release that demonstrably rebuilds this exact sealed store.

    Identity comes from the marker embedded in the sealed SQLite generation. Optional
    caller values are assertions only; they can reject a mismatched invocation but can
    never name another release. The partition comparison uses the publisher's row and
    coverage identity, so equal table counts cannot raise assurance on stale values.

    Stating the release alongside the sealed snapshot is what turns `source_assurance`
    into a distinction. The snapshot says which bytes were read; exact comparison says
    where those same rows can be read again.
    """

    try:
        origin = read_lake_store_origin(sqlite_path)
    except LakeStoreOriginError:
        return None
    if origin is None or mirror is None:
        return None
    assertions = (asserted_release_id, asserted_manifest_sha256)
    if any(value is not None for value in assertions) and assertions != (
        origin.release_id,
        origin.release_manifest_sha256,
    ):
        raise LakeBuildError("asserted L1 release does not match the sealed market store origin")
    try:
        with open_lake(mirror=mirror) as (_session, cache):
            return release_backing_store(
                cache.source,
                store=sqlite_path,
                release_id=origin.release_id,
                manifest_sha256=origin.release_manifest_sha256,
            )
    except (LakeObjectError, LakeReadError, OSError, ValueError):
        return None


def calibration_build_command(
    *,
    sqlite_path: Path,
    calibration_dir: Path,
    rules: ScreeningRules,
    start: date,
    end: date,
    l1_release: str | None = None,
    l1_manifest_sha256: str | None = None,
    force: bool = False,
    panel_variant: PanelVariant = "production",
    use_control_event_exits: bool = True,
    use_failure_exits: bool = True,
    stdout: TextIO | None = None,
) -> int:
    if (l1_release is None) != (l1_manifest_sha256 is None):
        print(
            "calibration build: --l1-release and --l1-manifest-sha256 must be provided together",
            file=sys.stderr,
        )
        return 1
    unreadable = unreadable_store_reason(sqlite_path)
    if unreadable is not None:
        print(f"calibration build: {unreadable}", file=sys.stderr)
        return 1
    publication = lake_writer_lock(calibration_dir)
    with publication:
        try:
            producer_commit = verified_git_commit()
        except (OSError, RuntimeError) as exc:
            print(f"calibration build: {exc}", file=sys.stderr)
            return 1
        try:
            expected_current = current_bundle_ref(calibration_dir)
        except (CalibrationCacheError, CalibrationLakeError) as exc:
            # A store that cannot describe what it serves is not a store to write into.
            # Repairing it in place would mean rebuilding over live data from an
            # inventory nothing can state, so the store stays exactly as it is and the
            # rebuild goes somewhere else, where it can be read before it replaces
            # anything.
            print(
                f"calibration build: {exc}; rebuild into a separate --calibration-dir "
                "and swap the directories once the new one reads",
                file=sys.stderr,
            )
            return 1
        discard_abandoned_generations(calibration_dir, stdout=stdout)
        work_dir = calibration_dir.with_name(
            f"{_GENERATION_PREFIX}{calibration_dir.name}.{uuid.uuid4().hex}"
        )
        if not force and calibration_dir.exists():
            copytree(calibration_dir, work_dir, copy_function=link)
        try:
            with sealed_sqlite_snapshot(sqlite_path=sqlite_path, mirror_root=work_dir) as snapshot:
                return _calibration_build_command(
                    snapshot=snapshot,
                    l1_release=_l1_release_source(
                        snapshot.path,
                        mirror=_market_mirror(sqlite_path),
                        asserted_release_id=l1_release,
                        asserted_manifest_sha256=l1_manifest_sha256,
                    ),
                    l1_mirror=_market_mirror(sqlite_path),
                    calibration_dir=calibration_dir,
                    work_dir=work_dir,
                    expected_current=expected_current,
                    producer_commit=producer_commit,
                    rules=rules,
                    start=start,
                    end=end,
                    force=force,
                    panel_variant=panel_variant,
                    use_control_event_exits=use_control_event_exits,
                    use_failure_exits=use_failure_exits,
                    stdout=stdout,
                )
        except LakeBuildError as exc:
            print(f"calibration build: {exc}", file=sys.stderr)
            return 1
        finally:
            if work_dir.exists():
                rmtree(work_dir)


_GENERATION_PREFIX = ".generation."


def discard_abandoned_generations(calibration_dir: Path, *, stdout: TextIO | None = None) -> None:
    """Remove work generations a killed build left beside the store.

    A generation directory is a sibling of the store rather than a child of it, because
    it is built by hard-linking the store into it. That places it outside every prefix
    the lake inventory and the collector walk, so a build killed mid-run leaves several
    hundred megabytes that no capacity figure accounts for and nothing ever reclaims.

    The writer lock is what makes this safe to do unconditionally: a generation can only
    be live while its build holds that lock, and this runs holding it.
    """

    parent = calibration_dir.parent
    if not parent.is_dir():
        return
    prefix = f"{_GENERATION_PREFIX}{calibration_dir.name}."
    for path in sorted(parent.iterdir()):
        if not path.name.startswith(prefix) or not path.is_dir():
            continue
        # A generation is a hard-link tree, so its apparent size counts bytes the store
        # still holds; what this reclaims is the tree, not necessarily the blocks. Report
        # both honestly, and report failure rather than assume the removal happened.
        linked_bytes = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
        rmtree(path, ignore_errors=True)
        out = stdout if stdout is not None else sys.stdout
        if path.exists():
            print(
                f"calibration build: could not discard abandoned generation {path.name}",
                file=sys.stderr,
            )
            continue
        print(
            f"calibration build: discarded abandoned generation {path.name} "
            f"({linked_bytes} linked bytes)",
            file=out,
        )


def _cohort_sources(
    snapshot: LegacySQLiteSnapshot, l1_release: L1ReleaseSourceRef | None
) -> tuple[CohortSourceRef, ...]:
    """What this cohort states it was built from, weakest claim first."""

    return (snapshot.ref,) if l1_release is None else (snapshot.ref, l1_release)


def _calibration_build_command(
    *,
    snapshot: LegacySQLiteSnapshot,
    l1_release: L1ReleaseSourceRef | None,
    l1_mirror: Path | None,
    calibration_dir: Path,
    work_dir: Path,
    expected_current: CalibrationBundleRef | None,
    producer_commit: str,
    rules: ScreeningRules,
    start: date,
    end: date,
    force: bool = False,
    panel_variant: PanelVariant = "production",
    use_control_event_exits: bool = True,
    use_failure_exits: bool = True,
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    policy = PANEL_BUILD_POLICIES[panel_variant]
    forward_policy = ForwardObservationPolicy(
        use_control_event_exits=use_control_event_exits, use_failure_exits=use_failure_exits
    )
    is_default_dir = calibration_dir.resolve() == DEFAULT_CALIBRATION_DIR.resolve()
    if not policy.production_authority and is_default_dir:
        print(
            "calibration build: diagnostic panel variant requires a separate --calibration-dir",
            file=sys.stderr,
        )
        return 1
    if not use_control_event_exits and is_default_dir:
        print(
            "calibration build: --without-control-event-exits builds a comparison baseline "
            "and requires a separate --calibration-dir",
            file=sys.stderr,
        )
        return 1
    if not use_failure_exits and is_default_dir:
        print(
            "calibration build: --without-failure-exits builds a comparison baseline "
            "and requires a separate --calibration-dir",
            file=sys.stderr,
        )
        return 1
    fixed_sqlite = snapshot.path
    asofs = month_end_asof_grid(fixed_sqlite, start=start, end=end)
    if not asofs:
        print("no month-end trading days found in the requested window", file=sys.stderr)
        return 1
    # Both sides of this comparison are known before a single cohort is computed, and a
    # full rebuild of this store takes 25 minutes. Refusing here rather than only after
    # the build is what keeps a too-narrow window from costing that time twice.
    if dropped := _cohorts_a_grid_would_drop(calibration_dir, asofs, force=force):
        print(_dropped_cohorts_message(dropped), file=sys.stderr)
        return 1
    tickers_by_asof: dict[date, set[str]] = {}
    built = 0
    expected_rules_hash = rules_content_hash(rules, policy)
    for asof in asofs:
        try:
            if has_cohort(work_dir, asof) and not force:
                meta = read_panel_meta(work_dir, asof)
                if (
                    meta.get("rules_hash") != expected_rules_hash
                    or meta.get("panel_variant") != policy.variant
                    or meta.get("production_authority") is not policy.production_authority
                    or meta.get("self_range_history_sessions") != policy.valuation_history_sessions
                    or meta.get("bars_input_window_days") != policy.bars_input_window_days
                ):
                    raise CalibrationCacheError(
                        "calibration panel contract differs from the requested build; "
                        "run calibration-build --force"
                    )
                tickers_by_asof[asof] = {row.ticker for row in read_panel(work_dir, asof)}
                continue
            result = build_panel(asof, sqlite_path=fixed_sqlite, rules=rules, policy=policy)
            write_panel(
                work_dir,
                asof,
                result.rows,
                result.diagnostics,
                sources=_cohort_sources(snapshot, l1_release),
                l1_mirror=l1_mirror,
                input_cutoff=asof,
                producer_commit=producer_commit,
                lock_held=True,
                forward_policy=forward_policy,
            )
        except (CalibrationError, CalibrationCacheError) as exc:
            print(f"calibration build: {asof.isoformat()} failed: {exc}", file=sys.stderr)
            return 1
        tickers_by_asof[asof] = {row.ticker for row in result.rows}
        built += 1
    control_event_exits = read_control_event_exits(fixed_sqlite) if use_control_event_exits else {}
    failure_exits = read_failure_exits(fixed_sqlite) if use_failure_exits else {}
    observation_cutoff = latest_market_data_date(fixed_sqlite)
    if observation_cutoff is None:
        print("calibration build: snapshot contains no market observation cutoff", file=sys.stderr)
        return 1
    by_asof: dict[str, list[ForwardReturnRow]] = {}
    for asof in asofs:
        for row in compute_forward_returns(
            fixed_sqlite,
            asofs=(asof,),
            tickers=tickers_by_asof[asof],
            control_event_exits=control_event_exits,
            failure_exits=failure_exits,
        ):
            by_asof.setdefault(row.asof, []).append(row)
    for asof in asofs:
        try:
            write_forward(
                work_dir,
                asof,
                by_asof.get(asof.isoformat(), []),
                sources=_cohort_sources(snapshot, l1_release),
                l1_mirror=l1_mirror,
                input_cutoff=observation_cutoff,
                producer_commit=producer_commit,
                lock_held=True,
                forward_policy=forward_policy,
            )
        except CalibrationCacheError as exc:
            print(f"calibration build: {asof.isoformat()} failed: {exc}", file=sys.stderr)
            return 1
    rows = [row for cohort_rows in by_asof.values() for row in cohort_rows]
    resolved = sum(row.resolved for row in rows)
    control_event = sum(row.status == CONTROL_EVENT_EXIT_STATUS for row in rows)
    failure_exit = sum(row.status == FAILURE_EXIT_STATUS for row in rows)
    dropped = _cohorts_this_build_would_drop(calibration_dir, work_dir, force=force)
    if dropped:
        print(_dropped_cohorts_message(dropped), file=sys.stderr)
        return 1
    adoption = adopt_bundle_generation(
        calibration_dir,
        work_dir,
        expected_current=expected_current,
        lock_held=True,
    )
    print(
        f"calibration build: done (panels built={built}, forward rows={len(rows)}, "
        f"resolved={resolved}, control event exits={control_event}, "
        f"failure exits={failure_exit})",
        file=out,
    )
    # What making the generation current cost. Printing it is how a run that starts
    # reinstalling the whole store instead of the month it changed becomes visible
    # before the wall time does.
    print(
        f"calibration build: adopted {adoption.bundle_id} "
        f"(closure objects={adoption.closure_objects}, hashed bytes={adoption.hashed_bytes}, "
        f"installed objects={adoption.installed_objects}, "
        f"installed bytes={adoption.installed_bytes}, reused objects={adoption.reused_objects})",
        file=out,
    )
    return 0


def _dropped_cohorts_message(dropped: Sequence[date]) -> str:
    return (
        "calibration build: this run would publish a generation without "
        f"{len(dropped)} cohort(s) the store holds: "
        f"{', '.join(item.isoformat() for item in dropped[:5])}"
        f"{' …' if len(dropped) > 5 else ''}. "
        "Widen --start/--end to cover them, or rebuild into a separate "
        "--calibration-dir if a shorter history is what you want."
    )


def _cohorts_a_grid_would_drop(
    calibration_dir: Path, asofs: Sequence[date], *, force: bool
) -> list[date]:
    """The same refusal as below, decided from the requested grid before anything is built.

    A full rebuild recomputes every cohort from the sealed snapshot, so learning that the
    window was too narrow only at adoption time throws away the whole run. The grid is not
    the authority on what the generation will hold — a cohort can fail to build — so this
    predicts rather than decides, and the check on the built generation still runs.
    """

    if not force:
        return []
    return sorted(set(published_cohorts(calibration_dir)) - set(asofs))


def _cohorts_this_build_would_drop(
    calibration_dir: Path, work_dir: Path, *, force: bool
) -> list[date]:
    """Cohorts the store serves now that the generation about to be adopted omits.

    A build states the window it recomputes, not the history it intends to keep. Without
    ``--force`` the store is hard-linked into the work generation, so everything outside
    the window is carried and this is empty by construction. With it the generation
    starts empty and holds exactly the requested as-ofs — so a run meant to correct one
    year would publish a current bundle holding only that year, and the other six would
    leave the served inventory without anything saying so.

    The check is on the built generation rather than on the requested grid: what matters
    is what is about to become current, whatever produced it.

    The store this runs against always resolves: a build that could not read what it
    was about to replace stopped before it started.
    """

    if not force:
        return []
    return sorted(set(published_cohorts(calibration_dir)) - set(published_cohorts(work_dir)))


def _optional_count(value: object) -> int | None:
    """Read a panel count, keeping "not measured" distinct from zero."""
    return value if type(value) is int else None


def _survivorship_status(mismatch: int | None) -> str:
    """Turn the population mismatch count into the cohort verdict.

    Deriving the verdict here rather than freezing it into the panel keeps a
    change in what counts as complete applicable to cohorts already on disk.
    """
    if mismatch is None:
        return "unavailable"
    return "complete" if mismatch == 0 else "incomplete"


def _required_metric_statuses(
    reported: object, required_metrics: tuple[str, ...]
) -> dict[str, str]:
    """Missing or unknown per-metric evidence cannot inherit cohort eligibility."""
    statuses = reported if isinstance(reported, dict) else {}
    return {
        metric: (
            str(statuses[metric])
            if statuses.get(metric) in {"eligible", "unresolved"}
            else "unresolved"
        )
        for metric in required_metrics
    }


def calibration_evaluate_command(
    *,
    calibration_dir: Path,
    horizons: list[str] | None = None,
    run_purpose: str = "diagnostic",
    required_asofs: list[str] | None = None,
    required_metrics: list[str] | None = None,
    output_path: Path | None = None,
    context_output_path: Path | None = None,
    start: date | None = None,
    end: date | None = None,
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    horizons = horizons or list(HORIZONS)
    unknown = [horizon for horizon in horizons if horizon not in HORIZONS]
    if unknown:
        print(f"unknown horizon(s): {', '.join(unknown)}", file=sys.stderr)
        return 1
    if run_purpose not in {"diagnostic", "production_decision"}:
        print("--run-purpose must be diagnostic or production_decision", file=sys.stderr)
        return 1
    if run_purpose == "production_decision":
        if not required_asofs or not required_metrics:
            print(
                "production_decision requires explicit --required-asof and --required-metric",
                file=sys.stderr,
            )
            return 1
        # The two rejections have different fixes, so they get different messages:
        # an operator who passed the core three and one unregistered name is told to
        # add the core three unless the unregistered name is stated.
        unknown_metrics = set(required_metrics) - KNOWN_METRICS
        if unknown_metrics:
            print(
                f"unknown required metrics: {', '.join(sorted(unknown_metrics))}",
                file=sys.stderr,
            )
            return 1
        if set(PRODUCTION_REQUIRED_METRICS) - set(required_metrics):
            print(
                "production_decision required metrics must include "
                f"{', '.join(PRODUCTION_REQUIRED_METRICS)}",
                file=sys.stderr,
            )
            return 1
    try:
        bundle = resolve_calibration_bundle(calibration_dir)
    except CalibrationCacheError as exc:
        print(f"calibration evaluate: {exc}", file=sys.stderr)
        return 1
    all_asofs = published_cohorts(calibration_dir, bundle=bundle)
    asofs = sorted(
        asof
        for asof in all_asofs
        if (start is None or asof >= start) and (end is None or asof <= end)
    )
    if not asofs:
        print(f"no panels found under {calibration_dir}", file=sys.stderr)
        return 1
    try:
        metas = [read_panel_meta(calibration_dir, asof, bundle=bundle) for asof in asofs]
        if len({meta.get("rules_hash") for meta in metas}) != 1:
            raise CalibrationCacheError(
                "panel store mixes rules provenance; run calibration-build --force"
            )
        panels = {
            asof.isoformat(): read_panel(calibration_dir, asof, bundle=bundle) for asof in asofs
        }
        if run_purpose == "production_decision" and not _is_production_panel_contract(
            metas, panels
        ):
            print(
                "calibration evaluate: diagnostic panel variant has no production authority",
                file=sys.stderr,
            )
            return 1
        forwards = {
            asof.isoformat(): read_forward(calibration_dir, asof, bundle=bundle) for asof in asofs
        }
    except CalibrationCacheError as exc:
        print(f"calibration evaluate: {exc}", file=sys.stderr)
        return 1
    # The conclusion is made of all three roles, so the cohort's assurance is the
    # weakest of them. Reading the panel alone would let a cohort whose outcomes came
    # from an unkept store generation be reported as rebuildable because its
    # cross-section happened to be migrated from an archive.
    cohort_assurance = {
        asof: source_assurance(
            (*entry.panel.sources, *entry.diagnostics.sources, *entry.forward.sources)
        )
        for asof, entry in bundle.cohorts.items()
    }
    results = evaluate_cohorts(panels, forwards, horizons=horizons)
    scope = EvaluationScope(
        run_purpose=run_purpose,
        requested_horizons=tuple(horizons),
        cohort_window={
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
        },
        required_asofs=tuple(required_asofs or [asof.isoformat() for asof in asofs]),
        required_metrics=tuple(required_metrics or PRODUCTION_REQUIRED_METRICS),
    )
    integrity: list[CohortIntegrity] = []
    integrity_reason_counts: dict[str, int] = {}
    for horizon, result in results.items():
        assert isinstance(result, dict)
        for cohort in result["cohorts"]:
            assert isinstance(cohort, dict)
            coverage = cast(dict[str, object], cohort["coverage"])
            blockers: list[str] = []
            meta = metas[[asof.isoformat() for asof in asofs].index(str(cohort["asof"]))]
            policy_reason_counts = meta.get("policy_exclusion_reason_counts")
            if not isinstance(policy_reason_counts, dict):
                policy_reason_counts = {}
            mismatch = _optional_count(meta.get("asof_population_mismatch_count"))
            unevaluated = _optional_count(meta.get("priced_master_without_universe_count"))
            coverage.update(
                {
                    "master_snapshot_date": meta.get("master_snapshot_date"),
                    "master_snapshot_status": meta.get("master_snapshot_status", "unavailable"),
                    "master_population_count": meta.get("master_population_count", 0),
                    "candidate_population_count": meta.get("candidate_population_count", 0),
                    "policy_exclusion_reason_counts": policy_reason_counts,
                    "policy_excluded_count": sum(
                        int(value)
                        for value in policy_reason_counts.values()
                        if isinstance(value, int)
                    ),
                    "input_range_clamped": bool(
                        meta.get("bars_window_clamped") or meta.get("fin_window_clamped")
                    ),
                    # EDINET の書類から作る軸を持つ母集団の行数。この source は最近の
                    # as-of 分しか store に無いので古い cohort は 0 になり、その cohort は
                    # production と同じ入力で screen を再現していない (production は同じ
                    # 軸を銘柄の 53〜64% で持つ)。件数を出し、判定は読み手が導く。
                    # `null` は計測前に書かれた panel で、0 (観測して 1 件も無い) と違う。
                    "edinet_axis_population_count": _optional_count(
                        meta.get("population_edinet_axis_nonnull")
                    ),
                    # Survivorship belongs to the population the panel drew, so the
                    # panel measures it and the reader turns the counts into the
                    # verdict. A panel written before the measurement existed
                    # reports null rather than zero, so "not measured" cannot be
                    # read as "nothing missing".
                    "survivorship_coverage_status": _survivorship_status(mismatch),
                    "asof_priced_count": meta.get("asof_priced_count"),
                    "asof_population_mismatch_count": mismatch,
                    "policy_excluded_priced_count": meta.get("policy_excluded_priced_count"),
                    "priced_master_without_universe_count": unevaluated,
                    "entry_resolution_lag_days": meta.get("entry_resolution_lag_days"),
                    "panel_variant": meta.get("panel_variant", "unknown"),
                    "production_authority": meta.get("production_authority"),
                    "self_range_history_sessions": meta.get("self_range_history_sessions"),
                    "self_range_degraded_count": sum(
                        1 for row in panels[str(cohort["asof"])] if row.self_range_degraded
                    ),
                }
            )
            coverage["source_assurance"] = cohort_assurance.get(str(cohort["asof"]), "trace_only")
            if horizon in {"3y", "5y"}:
                # Reading a stored result again and recomputing it from its input are
                # different capabilities. A decision that changes the production method
                # has to survive being re-derived — after a logic error, after a rules
                # revision — and only a cohort whose upstream input the lake keeps can
                # be. The archive of a previous producer's output is not that input, so
                # it states its own level rather than passing as one.
                #
                # This is the only blocker a diagnostic run does not take. The others
                # describe the cohort itself and hold whoever is reading it; this one
                # describes what may be changed on the strength of the cohort, which is
                # a question a diagnostic run is not asking.
                if (
                    run_purpose == "production_decision"
                    and coverage["source_assurance"] != "rebuildable_input"
                ):
                    blockers.append("source_not_rebuildable")
                # One blocker per independent observation. A verdict derived from
                # another observation would count the same gap twice and make the
                # reason histogram unreadable.
                if meta.get("master_snapshot_status") != "exact_date":
                    blockers.append("master_snapshot")
                if coverage["survivorship_coverage_status"] != "complete":
                    blockers.append("survivorship")
                if coverage.get("adjustment_factor_coverage") != "complete":
                    blockers.append("adjustment_factor")
                if coverage["input_range_clamped"]:
                    blockers.append("input_range_clamped")
                if not coverage.get("candidate_partition_complete"):
                    blockers.append("candidate_partition_incomplete")
                unevaluated_sensitivity = coverage.get("priced_master_without_universe")
                if (
                    not isinstance(unevaluated, int)
                    or not isinstance(unevaluated_sensitivity, dict)
                    or unevaluated_sensitivity.get("excluded_count") != unevaluated
                ):
                    blockers.append("priced_master_without_universe_unmeasured")
                elif not unevaluated_sensitivity.get("direction_stable"):
                    blockers.append("priced_master_without_universe_flips_direction")
                # Each unresolved class blocks for its own reason, and a name the
                # panel could not price at asof blocks for none of them. The
                # residual keeps an unenumerated status from passing silently.
                for field, label in (
                    ("entry_price_gap_count", "entry_price_gap"),
                    ("future_horizon_count", "horizon_not_matured"),
                    ("unclassified_unresolved_count", "unclassified_unresolved"),
                ):
                    count = coverage.get(field)
                    if isinstance(count, int) and count:
                        blockers.append(label)
                # A name that left the market carries no exit value, and delistings
                # happen in every cohort, so blocking on their presence blocks
                # forever. What matters is whether they could have produced the
                # conclusions: the cohort blocks when the sign of a conclusion moves
                # between giving those names a total loss and giving them what the
                # rest of the cohort returned. Pre-registered in
                # reports/studies/2026-07-31-delisting-exclusion/preregistration.md.
                sensitivity = coverage.get("delisting_exclusion")
                if isinstance(sensitivity, dict) and not sensitivity.get("direction_stable"):
                    blockers.append("unpriced_exit_flips_direction")
                optional_required = set(scope.required_metrics).intersection(
                    OPTIONAL_SENSITIVITY_METRICS
                )
                for coverage_key, reason_prefix in (
                    ("delisting_exclusion", "unpriced_exit_flips"),
                    ("priced_master_without_universe", "priced_master_without_universe_flips"),
                ):
                    sensitivity = coverage.get(coverage_key)
                    metric_stability = (
                        sensitivity.get("metric_direction_stable")
                        if isinstance(sensitivity, dict)
                        else None
                    )
                    for metric in sorted(optional_required):
                        if (
                            not isinstance(metric_stability, dict)
                            or metric_stability.get(metric) is not True
                        ):
                            blockers.append(f"{reason_prefix}:{metric}")
            metric_status = (
                "eligible" if cohort["metric_calculation_status"] == "resolved" else "unresolved"
            )
            integrity.append(
                CohortIntegrity(
                    asof=str(cohort["asof"]),
                    horizon=horizon,
                    integrity_status=("blocked" if blockers else metric_status),
                    metric_statuses=_required_metric_statuses(
                        cohort.get("metric_statuses"), scope.required_metrics
                    ),
                    blocking_reasons=tuple(blockers),
                )
            )
            for reason in blockers:
                integrity_reason_counts[reason] = integrity_reason_counts.get(reason, 0) + 1
    decision = decide_authority(scope, tuple(integrity))
    required_pairs = {(asof, horizon) for asof in scope.required_asofs for horizon in ("3y", "5y")}
    required_integrity = [item for item in integrity if (item.asof, item.horizon) in required_pairs]
    eligible_pairs = {
        (item.asof, item.horizon)
        for item in required_integrity
        if item.integrity_status == "eligible"
        and all(item.metric_statuses.get(metric) == "eligible" for metric in scope.required_metrics)
    }
    # The histogram answers "how many required cohorts show this", so the per-cohort
    # verdicts are counted here rather than taken from the decision's deduped classes.
    for item in required_integrity:
        if item.integrity_status != "eligible":
            key = f"integrity_{item.integrity_status}"
            integrity_reason_counts[key] = integrity_reason_counts.get(key, 0) + 1
        for metric in scope.required_metrics:
            if item.metric_statuses.get(metric) != "eligible":
                key = f"metric_unresolved:{metric}"
                integrity_reason_counts[key] = integrity_reason_counts.get(key, 0) + 1
    # Scope-level reasons hold for the run as a whole, so they count once.
    for reason in decision.blocking_reasons:
        integrity_reason_counts.setdefault(reason, 1)
    payload = {
        "kind": "estimate-calibration-evaluation",
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "screening_rules_hash": metas[0].get("rules_hash"),
        "er_model_version": EXPECTED_RETURN_MODEL_VERSION,
        "metric_basis": "price_return_only",
        "metric_bases": ["price_return_only", "fy_actual_dividend_total_return"],
        "scope": {
            "run_purpose": scope.run_purpose,
            "requested_horizons": list(scope.requested_horizons),
            "cohort_window": scope.cohort_window,
            "required_asofs": list(scope.required_asofs),
            "required_metrics": list(scope.required_metrics),
        },
        "production_decision": decision.payload(),
        "authority_coverage": {
            "required_cohort_count": len(required_pairs),
            "eligible_metric_cohort_count": len(eligible_pairs),
            "blocked_or_unresolved_count": len(required_pairs - eligible_pairs),
            "reason_counts": integrity_reason_counts,
        },
        "cohort_integrity": [asdict(item) for item in integrity],
        "results": results,
    }
    context_payload: dict[str, object] | None = None
    if context_output_path is not None:
        try:
            context_payload = build_er_distribution_context(payload, panels, forwards)
        except CalibrationContextError as exc:
            print(f"calibration evaluate: context not written: {exc}", file=sys.stderr)
            return 1
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    if output_path:
        write_text_atomic(output_path, text)
        print(f"calibration evaluate: wrote {output_path}", file=out)
    else:
        print(text, file=out)
    if context_output_path is not None and context_payload is not None:
        write_text_atomic(
            context_output_path,
            yaml.safe_dump(context_payload, sort_keys=False, allow_unicode=True),
        )
        print(f"calibration evaluate: wrote {context_output_path}", file=out)
    return 0


def _is_production_panel_contract(
    metas: list[dict[str, object]], panels: dict[str, list[PanelRow]]
) -> bool:
    policy = PRODUCTION_PANEL_POLICY
    return all(
        meta.get("panel_variant") == policy.variant
        and meta.get("production_authority") is policy.production_authority
        and meta.get("self_range_history_sessions") == policy.valuation_history_sessions
        and meta.get("bars_input_window_days") == policy.bars_input_window_days
        for meta in metas
    ) and all(not row.self_range_degraded for rows in panels.values() for row in rows)
