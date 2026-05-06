from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
POLICY_EFFECTIVE_FROM = "2026-05-01T00:00:00+09:00"
POLICY_PATH = Path("records/01-policy/2026/05/2026-05-01T000000+0900-portfolio-policy.md")
SCREENING_RULES_PATH = Path("records/_config/screening-rules/2026-05-01T000000+0900.yaml")
METRIC_CATALOG_PATH = Path("records/_config/metric-catalog/2026-05-01T000000+0900.yaml")
EXPOSURE_BUCKETS_PATH = Path("records/_config/exposure-buckets/2026-05-01T000000+0900.yaml")
UNIVERSE_SNAPSHOT_PATH = Path("records/_universe-snapshots/2026/05/2026-05-01T192150+0900.yaml")
BUSINESS_DAY_CALENDAR_PATH = Path("records/_calendars/business-days/2026-05.yaml")
EVENT_CALENDAR_PATH = Path("records/_calendars/events/2026-05.yaml")
CORPORATE_ACTION_CALENDAR_PATH = Path("records/_calendars/corporate-actions/2026-05.yaml")
MARKET_DATA_PATH = Path("records/_market-data/2026/05/2026-05-01-close.yaml")
SECTOR_BASELINE_PATH = Path("records/_config/sector-baselines/2026-05-01T000000+0900.yaml")
APPROVAL_RULES_PATH = Path("records/_approval-rules/2026-05-01T000000+0900.yaml")
PORTFOLIO_EXPOSURE_1330_PATH = Path(
    "records/_portfolio-exposure/2026/05/2026-05-05T133000+0900.yaml"
)
PORTFOLIO_EXPOSURE_2000_PATH = Path(
    "records/_portfolio-exposure/2026/05/2026-05-05T200000+0900.yaml"
)
PORTFOLIO_EXPOSURE_2030_PATH = Path(
    "records/_portfolio-exposure/2026/05/2026-05-05T203000+0900.yaml"
)

PLAYBOOK_FAMILY: dict[str, list[str]] = {
    "valuation-reversion": ["valuation", "market_derived", "positioning_liquidity"],
    "strict-net-cash-discount": ["valuation", "fundamental"],
    "cash-rich-asset-discount": ["valuation", "fundamental"],
    "cashflow-yield-discount": ["valuation", "fundamental"],
    "fcf-yield-discount": ["valuation", "fundamental"],
    "sales-discount-growth": ["valuation", "fundamental"],
}

PLAYBOOK_COMPONENT: dict[str, str] = {
    "valuation-reversion": "market-price-reversion",
    "strict-net-cash-discount": "balance-sheet-net-cash",
    "cash-rich-asset-discount": "balance-sheet-cash-rich",
    "cashflow-yield-discount": "cashflow-operating-yield",
    "fcf-yield-discount": "cashflow-free-cash-yield",
    "sales-discount-growth": "sales-discount-growth",
}

PLAYBOOK_CLAIM_TYPE: dict[str, str] = {
    "valuation-reversion": "valuation_reversion",
    "strict-net-cash-discount": "balance_sheet_discount",
    "cash-rich-asset-discount": "balance_sheet_discount",
    "cashflow-yield-discount": "cashflow_yield",
    "fcf-yield-discount": "free_cashflow_yield",
    "sales-discount-growth": "sales_discount_growth",
}

RESEARCH_PAYOFF: dict[str, dict[str, Any]] = {
    "3678": {
        "max_entry_price_yen": 1225,
        "target_price_yen": 1400,
        "stop_loss_yen": 1050,
        "time_horizon_bd": 40,
        "invalidation_conditions": [
            (
                "Seven Seas acquisition and borrowing leave pro-forma net cash below "
                "the thesis threshold."
            )
        ],
    },
    "6310": {
        "max_entry_price_yen": 1750,
        "target_price_yen": 2100,
        "stop_loss_yen": 1550,
        "time_horizon_bd": 40,
        "invalidation_conditions": ["Post-1Q operating cash-flow thesis deteriorates."],
    },
    "6835": {
        "max_entry_price_yen": 270,
        "target_price_yen": 340,
        "stop_loss_yen": 230,
        "time_horizon_bd": 40,
        "invalidation_conditions": ["Q1 invalidates FCF or cash-flow durability."],
    },
    "7613": {
        "max_entry_price_yen": 1300,
        "target_price_yen": 1550,
        "stop_loss_yen": 1150,
        "time_horizon_bd": 40,
        "invalidation_conditions": ["FCF strength is explained by working-capital timing only."],
    },
    "9682": {
        "max_entry_price_yen": 1050,
        "target_price_yen": 1230,
        "stop_loss_yen": 950,
        "time_horizon_bd": 40,
        "invalidation_conditions": [
            "Shareholder-return catalyst is absorbed and price breaks below 950 yen."
        ],
    },
    "9692": {
        "max_entry_price_yen": 2000,
        "target_price_yen": 2300,
        "stop_loss_yen": 1800,
        "time_horizon_bd": 40,
        "invalidation_conditions": ["Growth slowdown overwhelms P/S and dividend support."],
    },
}


@dataclass(frozen=True)
class SnapshotRef:
    ref_path: str
    content_sha256: str
    effective_from: str | None = None

    def as_dict(self) -> dict[str, str]:
        data = {"ref_path": self.ref_path, "content_sha256": self.content_sha256}
        if self.effective_from is not None:
            data["effective_from"] = self.effective_from
        return data


def main() -> None:
    create_policy_and_snapshots()
    migrate_playbooks()
    refs = build_snapshot_refs()
    migrate_outlooks()
    candidate_index = migrate_candidates(refs)
    research_meta = migrate_research(refs, candidate_index)
    migrate_trades(refs)
    migrate_ledger(refs, research_meta, candidate_index)
    create_review_artifacts()
    create_migration_manifest(refs)


def write_text(path: Path, text: str) -> None:
    full = ROOT / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(text, encoding="utf-8")


def write_yaml(path: Path, payload: Any) -> None:
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False)
    write_text(path, text)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    write_text(path, text)


def content_sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


def snapshot_ref(path: Path, *, effective_from: str | None = None) -> SnapshotRef:
    return SnapshotRef(
        ref_path=str(path),
        content_sha256=content_sha(path),
        effective_from=effective_from,
    )


def yaml_load(path: Path) -> Any:
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def create_policy_and_snapshots() -> None:
    policy = """---
policy_id: portfolio-policy
effective_from: "2026-05-01T00:00:00+09:00"
policy_origin: codified_post_hoc
objectives:
  primary: "お買い得銘柄を拾う取引判断の精度を高める"
  evaluation_horizon_bd: 30
  minimum_horizon_relative_return_pct_vs_topix: 5.0
eligible_instruments:
  markets: ["JPX"]
  instruments: ["cash_equity"]
capital_basis:
  real_capital_yen: 5000000
  tactical_real_budget_yen: 1000000
  paper_proxy_capital_yen: 100000000
risk_budget:
  max_paper_proxy_position_size_yen: 1000000
  max_real_order_notional_yen: 250000
  max_ticker_real_concentration_pct: 8.0
  max_sector_real_concentration_pct: 45.0
  max_playbook_real_concentration_pct: 35.0
  max_economic_exposure_real_concentration_pct: 40.0
minimum_payoff:
  min_risk_reward_ratio: 1.1
  min_expected_upside_pct: 10.0
execution_scaling:
  paper_to_real_order_notional_pct: 21.0
  scaling_method: codified_post_hoc_constant
  recalibration_trigger: first_native_policy_revision
macro_regime_policy:
  adverse_treatment: conditional
  adverse_exception_cap_pct_of_default: 40.0
conviction_tier_caps:
  low:
    max_paper_proxy_position_size_yen: 500000
    max_real_order_notional_yen: 100000
  medium:
    max_paper_proxy_position_size_yen: 1000000
    max_real_order_notional_yen: 250000
  high:
    max_paper_proxy_position_size_yen: 2000000
    max_real_order_notional_yen: 500000
conviction_tier_rules:
  count_breadth:
    high_min_independent_evidence_count: 3
    medium_min_independent_evidence_count: 1
  high_depth:
    min_independent_evidence_count: 1
    requires_depth_verification_ref: true
    min_risk_reward_ratio: 2.0
    requires_disconfirming_or_risk_evidence: true
sizing_ladder:
  low:
    default_paper_proxy_position_size_yen: 500000
  medium:
    default_paper_proxy_position_size_yen: 1000000
  high:
    default_paper_proxy_position_size_yen: 1500000
order_constraints:
  board_lot: 100
  price_guard_required: true
review_cycle:
  plus_15bd: true
  plus_30bd: true
  time_stop_review: true
policy_rules:
  __default__:
    override_allowed: false
    override_severity: error
  minimum_payoff.min_risk_reward_ratio:
    override_allowed: true
    override_severity: warning
    requires_rationale_ref: true
    requires_review_flag: true
  minimum_payoff.min_expected_upside_pct:
    override_allowed: true
    override_severity: warning
    requires_rationale_ref: true
    requires_review_flag: true
---

# Portfolio Policy

この snapshot は自己運用の判断統制に使う portfolio policy であり、投資助言や自動売買ルールではない。
"""
    write_text(POLICY_PATH, policy)
    write_jsonl(
        Path("records/01-policy/_changelog.jsonl"),
        [
            {
                "event_id": "policy-change-20260501-initial",
                "event_type": "created",
                "policy_id": "portfolio-policy",
                "snapshot_path": str(POLICY_PATH),
                "effective_from": POLICY_EFFECTIVE_FROM,
                "recorded_at": "2026-05-06T00:00:00+09:00",
            }
        ],
    )
    write_yaml(
        Path("records/01-policy/_index.yaml"),
        {
            "portfolio-policy": {
                "latest_snapshot": str(POLICY_PATH),
                "effective_from": POLICY_EFFECTIVE_FROM,
            }
        },
    )

    old_rules = ROOT / "records/_config/screening-rules.yaml"
    rules = yaml.safe_load(old_rules.read_text(encoding="utf-8")) if old_rules.exists() else {}
    if isinstance(rules, dict) and "signal_lanes" in rules:
        rules["screening_playbooks"] = rules.pop("signal_lanes")
        for playbook in rules["screening_playbooks"].values():
            if isinstance(playbook, dict) and "playbook" in playbook:
                playbook["playbook_id"] = playbook.pop("playbook")
    write_yaml(SCREENING_RULES_PATH, rules)
    if old_rules.exists():
        old_rules.unlink()

    metric_catalog = {
        "schema": "metric-catalog",
        "effective_from": POLICY_EFFECTIVE_FROM,
        "metrics": [
            {
                "metric_id": "p_s",
                "evidence_family": "valuation",
                "economic_exposure_group": "sales-valuation",
                "source_snapshot_group": "market-and-sales",
                "freshness_dependency": "financial_statement",
                "max_valid_days": 220,
            },
            {
                "metric_id": "sales_yoy",
                "evidence_family": "fundamental",
                "economic_exposure_group": "sales-growth",
                "source_snapshot_group": "financial-statement",
                "freshness_dependency": "financial_statement",
                "max_valid_days": 220,
            },
            {
                "metric_id": "net_cash_to_market_cap",
                "evidence_family": "fundamental",
                "economic_exposure_group": "balance-sheet-cash",
                "source_snapshot_group": "balance-sheet",
                "freshness_dependency": "balance_sheet",
                "max_valid_days": 120,
            },
            {
                "metric_id": "ocf_yield",
                "evidence_family": "fundamental",
                "economic_exposure_group": "cashflow-yield",
                "source_snapshot_group": "cashflow-statement",
                "freshness_dependency": "financial_statement",
                "max_valid_days": 220,
            },
            {
                "metric_id": "fcf_yield",
                "evidence_family": "fundamental",
                "economic_exposure_group": "cashflow-yield",
                "source_snapshot_group": "cashflow-statement",
                "freshness_dependency": "financial_statement",
                "max_valid_days": 220,
            },
            {
                "metric_id": "sector_relative_strength_percentile",
                "evidence_family": "market_derived",
                "economic_exposure_group": "price-momentum",
                "source_snapshot_group": "market-price",
                "freshness_dependency": "market_data",
                "max_valid_days": 5,
            },
        ],
    }
    write_yaml(METRIC_CATALOG_PATH, metric_catalog)
    exposure_buckets = {
        "schema": "exposure-buckets",
        "effective_from": POLICY_EFFECTIVE_FROM,
        "buckets": [
            {"id": "japan-domestic-demand", "description": "Japan domestic demand"},
            {"id": "japan-external-demand", "description": "Japan external demand"},
            {"id": "us-demand", "description": "United States demand and rates"},
            {"id": "emerging-demand", "description": "Emerging market demand"},
            {"id": "global-tech-cycle", "description": "Global technology cycle"},
            {"id": "energy-price", "description": "Oil and energy price exposure"},
        ],
        "reducer_rule": {
            "materiality_included": ["high", "medium"],
            "low_materiality_role": "attribution_only",
            "unknown_confidence_effect": "conditional",
        },
    }
    write_yaml(EXPOSURE_BUCKETS_PATH, exposure_buckets)
    write_jsonl(
        Path("records/_config/_changelog.jsonl"),
        [
            {
                "event_id": "config-change-20260501-screening-rules",
                "snapshot_path": str(SCREENING_RULES_PATH),
                "effective_from": POLICY_EFFECTIVE_FROM,
            },
            {
                "event_id": "config-change-20260501-metric-catalog",
                "snapshot_path": str(METRIC_CATALOG_PATH),
                "effective_from": POLICY_EFFECTIVE_FROM,
            },
            {
                "event_id": "config-change-20260501-exposure-buckets",
                "snapshot_path": str(EXPOSURE_BUCKETS_PATH),
                "effective_from": POLICY_EFFECTIVE_FROM,
            },
        ],
    )
    write_yaml(
        BUSINESS_DAY_CALENDAR_PATH,
        {
            "calendar_id": "jpx-business-days-2026-05",
            "market": "JPX",
            "covered_from": "2026-05-01",
            "covered_until": "2026-05-31",
            "last_refreshed_at": "2026-05-05T00:00:00+09:00",
            "source_refs": [
                "records/_data/raw/screening/jquants/get_mkt_calendar-from_yyyymmdd-20260501-to_yyyymmdd-20260501.json"
            ],
            "holidays": ["2026-05-05", "2026-05-06"],
        },
    )
    write_yaml(
        EVENT_CALENDAR_PATH,
        {
            "calendar_id": "event-calendar-2026-05",
            "covered_from": "2026-05-01",
            "covered_until": "2026-05-31",
            "last_refreshed_at": "2026-05-05T00:00:00+09:00",
            "source_status": "ok",
            "events": [
                {"event_id": "us-employment-20260508", "date": "2026-05-08", "kind": "macro"},
                {"event_id": "us-cpi-20260512", "date": "2026-05-12", "kind": "macro"},
                {"event_id": "6835-earnings-20260515", "date": "2026-05-15", "kind": "earnings"},
                {"event_id": "6310-earnings-20260515", "date": "2026-05-15", "kind": "earnings"},
            ],
        },
    )
    write_yaml(
        CORPORATE_ACTION_CALENDAR_PATH,
        {
            "calendar_id": "corporate-actions-2026-05",
            "covered_from": "2026-03-01",
            "covered_until": "2026-05-31",
            "last_refreshed_at": "2026-05-05T00:00:00+09:00",
            "source_status": "ok",
            "events": [
                {
                    "event_id": "3678-seven-seas-acquisition-20260302",
                    "ticker": "3678",
                    "event_date": "2026-03-02",
                    "event_kind": "corporate_action_post_snapshot",
                    "title": "Seven Seas Entertainment acquisition",
                    "invalidates_metrics": ["net_cash_to_market_cap", "cash_to_market_cap"],
                },
                {
                    "event_id": "9682-buyback-20260501",
                    "ticker": "9682",
                    "event_date": "2026-05-01",
                    "event_kind": "share_buyback",
                    "title": "Share repurchase and cancellation",
                    "invalidates_metrics": [],
                },
            ],
        },
    )
    write_yaml(
        SECTOR_BASELINE_PATH,
        {
            "snapshot_id": "sector-baseline-20260501",
            "as_of": "2026-05-01",
            "baselines": {
                "TOPIX": {"close": None},
                "情報・通信業": {"return_30bd_pct": None},
            },
        },
    )
    write_yaml(
        APPROVAL_RULES_PATH,
        {
            "registry_id": "approval-rules-20260501",
            "effective_from": POLICY_EFFECTIVE_FROM,
            "rules": [
                {
                    "approval_rule_id": "analyst-event-evidence-reviewed",
                    "created_at": POLICY_EFFECTIVE_FROM,
                    "creation_motive": "prospective_policy",
                    "target_record_refs": [],
                    "max_valid_days": 30,
                }
            ],
        },
    )
    write_jsonl(
        Path("records/_approval-rules/_changelog.jsonl"),
        [
            {
                "event_id": "approval-rule-change-20260501-initial",
                "snapshot_path": str(APPROVAL_RULES_PATH),
                "effective_from": POLICY_EFFECTIVE_FROM,
            }
        ],
    )


def migrate_playbooks() -> None:
    root = ROOT / "records/_playbooks"
    playbook_files = sorted(root.glob("*.md"))
    for playbook_file in playbook_files:
        if playbook_file.name == "README.md":
            continue
        playbook_id = playbook_file.stem
        snapshot_path = Path("records/_playbooks") / playbook_id / "2026-05-01T000000+0900.md"
        text = playbook_file.read_text(encoding="utf-8")
        text = text.replace("playbook_id:", "playbook_id:")
        text = text.replace(
            "single evidence path / multiple independent evidence paths",
            "single evidence path / multiple independent evidence paths",
        )
        text = text.replace(
            "single signal / multiple signal",
            "single evidence path / multiple independent evidence paths",
        )
        text = text.replace("Crowding (positioning / liquidity)", "Positioning / liquidity")
        text = text.replace(
            "Price reaction と Crowding", "Price reaction and positioning / liquidity"
        )
        write_text(snapshot_path, text)
        schema_file = root / f"{playbook_id}.schema.yaml"
        if schema_file.exists():
            schema = yaml.safe_load(schema_file.read_text(encoding="utf-8"))
            if isinstance(schema, dict):
                for section in schema.get("body_sections", []):
                    if isinstance(section, dict) and section.get("title_pattern") == "Crowding":
                        section["title_pattern"] = "Positioning / liquidity|Crowding"
            write_yaml(Path("records/_playbooks") / playbook_id / "body-schema.yaml", schema)
            schema_file.unlink()
        playbook_file.unlink()
    readme = root / "README.md"
    readme.write_text(
        """# records/_playbooks/

Baibai-Loop で運用中の playbook snapshot 集合。
各 historical record は dated immutable snapshot を参照する。

## Snapshot layout

`records/_playbooks/<playbook_id>/YYYY-MM-DDTHHMMSS+0900.md`

Research / decision register / trade は snapshot path と content hash を持つ。
""",
        encoding="utf-8",
    )


def build_snapshot_refs() -> dict[str, SnapshotRef]:
    refs = {
        "policy": snapshot_ref(POLICY_PATH, effective_from=POLICY_EFFECTIVE_FROM),
        "screening_rules": snapshot_ref(SCREENING_RULES_PATH, effective_from=POLICY_EFFECTIVE_FROM),
        "metric_catalog": snapshot_ref(METRIC_CATALOG_PATH, effective_from=POLICY_EFFECTIVE_FROM),
        "exposure_buckets": snapshot_ref(
            EXPOSURE_BUCKETS_PATH, effective_from=POLICY_EFFECTIVE_FROM
        ),
        "universe": snapshot_ref(UNIVERSE_SNAPSHOT_PATH)
        if (ROOT / UNIVERSE_SNAPSHOT_PATH).exists()
        else SnapshotRef(str(UNIVERSE_SNAPSHOT_PATH), "sha256:pending"),
        "business_day_calendar": snapshot_ref(BUSINESS_DAY_CALENDAR_PATH),
        "event_calendar": snapshot_ref(EVENT_CALENDAR_PATH),
        "corporate_action_calendar": snapshot_ref(CORPORATE_ACTION_CALENDAR_PATH),
        "sector_baseline": snapshot_ref(SECTOR_BASELINE_PATH),
        "approval_rules": snapshot_ref(APPROVAL_RULES_PATH, effective_from=POLICY_EFFECTIVE_FROM),
    }
    for playbook_dir in sorted((ROOT / "records/_playbooks").iterdir()):
        if not playbook_dir.is_dir():
            continue
        snapshot = playbook_dir / "2026-05-01T000000+0900.md"
        refs[f"playbook:{playbook_dir.name}"] = snapshot_ref(
            snapshot.relative_to(ROOT), effective_from=POLICY_EFFECTIVE_FROM
        )
    return refs


def migrate_outlooks() -> None:
    for path in sorted((ROOT / "records/03-outlook").rglob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            continue
        if "regions" in doc:
            doc["exposure_buckets"] = doc.pop("regions")
        doc["artifact_provenance"] = {
            "source_refs": doc.get("updated_from", []),
            "source_status": "ok",
            "lineage": "brief-derived",
        }
        doc["macro_regime"] = {
            "aggregate_status": "mixed",
            "inputs": [
                {
                    "scope": "sector",
                    "key": sector,
                    "status": values.get("status") if isinstance(values, dict) else None,
                }
                for sector, values in (doc.get("sectors") or {}).items()
            ],
        }
        write_yaml(path.relative_to(ROOT), doc)


def migrate_candidates(refs: dict[str, SnapshotRef]) -> dict[str, dict[str, Any]]:
    candidate_index: dict[str, dict[str, Any]] = {}
    for path in sorted((ROOT / "records/04-candidates").rglob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            continue
        run_id = str(doc.get("run_id"))
        doc["screening_rules_snapshot"] = refs["screening_rules"].as_dict()
        doc["metric_catalog_snapshot"] = refs["metric_catalog"].as_dict()
        doc["policy_snapshot"] = refs["policy"].as_dict()
        doc["universe_snapshot_ref"] = {
            "ref_path": str(UNIVERSE_SNAPSHOT_PATH),
            "content_sha256": "sha256:pending",
        }
        doc["playbook_set_hash"] = _hash_text(
            ",".join(sorted(k.removeprefix("playbook:") for k in refs if k.startswith("playbook:")))
        )
        if "config_hash" in doc:
            doc.pop("config_hash")
        if "signals_summary" in doc:
            doc["evidence_hits_summary"] = doc.pop("signals_summary")
        candidates = doc.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                ticker = str(candidate.get("ticker"))
                candidate_id = f"candidate-{doc.get('run_date')}-{ticker}"
                candidate["screen_run_id"] = run_id
                candidate["candidate_id"] = candidate_id
                candidate["candidate_key"] = f"{run_id}:{ticker}"
                candidate["playbook_screen_result"] = "hit"
                candidate["policy_gate_result"] = "pass"
                candidate["liquidity_gate_result"] = "pass"
                candidate["macro_regime_gate_result"] = "pass"
                candidate["security_exposures"] = _security_exposures(candidate)
                evidence_hits = []
                for raw_hit in candidate.pop("signals", []) or []:
                    if not isinstance(raw_hit, dict):
                        continue
                    playbook_id = str(raw_hit.get("playbook") or raw_hit.get("name"))
                    evidence_hit_id = f"{candidate_id}-{playbook_id}"
                    metrics = (
                        raw_hit.get("metrics") if isinstance(raw_hit.get("metrics"), dict) else {}
                    )
                    hit = {
                        "evidence_hit_id": evidence_hit_id,
                        "playbook_id": playbook_id,
                        "claim_id": f"{ticker}-{PLAYBOOK_CLAIM_TYPE.get(playbook_id, playbook_id)}",
                        "claim_type": PLAYBOOK_CLAIM_TYPE.get(playbook_id, "screening_rule_hit"),
                        "evidence_family_set": PLAYBOOK_FAMILY.get(playbook_id, ["valuation"]),
                        "evidence_polarity": "supports",
                        "decision_role": "sizing_evidence",
                        "source_metric_ids": sorted(str(key) for key in metrics),
                        "correlation_group": PLAYBOOK_COMPONENT.get(playbook_id, playbook_id),
                        "independence_component_id": PLAYBOOK_COMPONENT.get(
                            playbook_id, playbook_id
                        ),
                        "source_snapshot_group": _source_snapshot_group(playbook_id),
                        "economic_exposure_group": PLAYBOOK_COMPONENT.get(playbook_id, playbook_id),
                        "freshness_dependency": _freshness_dependency(playbook_id),
                        "source_status": "ok",
                        "sizing_eligible": True,
                        "observed_at": doc.get("run_at"),
                        "snapshot_at": _metric_snapshot_at(candidate),
                        "metrics": metrics,
                        "reasons": raw_hit.get("reasons", []),
                    }
                    evidence_hits.append(hit)
                candidate["evidence_hits"] = evidence_hits
                candidate_index[ticker] = {
                    "candidate_id": candidate_id,
                    "candidate_key": candidate["candidate_key"],
                    "candidates_ref": str(path.relative_to(ROOT)),
                    "evidence_hit_ids": [hit["evidence_hit_id"] for hit in evidence_hits],
                    "evidence_by_playbook": {
                        str(hit["playbook_id"]): str(hit["evidence_hit_id"])
                        for hit in evidence_hits
                    },
                    "candidate": candidate,
                }
        write_yaml(path.relative_to(ROOT), doc)
        create_universe_snapshot(doc)
    return candidate_index


def create_universe_snapshot(candidates_doc: dict[str, Any]) -> None:
    members = []
    for candidate in candidates_doc.get("candidates", []) or []:
        if not isinstance(candidate, dict):
            continue
        members.append(
            {
                "ticker": candidate.get("ticker"),
                "name": candidate.get("name"),
                "sector_33": candidate.get("sector_33"),
                "market_cap_oku": candidate.get("market_cap_oku"),
                "avg_turnover_oku": candidate.get("avg_turnover_oku"),
                "security_exposures": candidate.get("security_exposures", []),
            }
        )
    write_yaml(
        UNIVERSE_SNAPSHOT_PATH,
        {
            "snapshot_id": "universe-20260501",
            "as_of": candidates_doc.get("asof_date"),
            "run_at": candidates_doc.get("run_at"),
            "universe_size": candidates_doc.get("universe_size"),
            "members_recorded": len(members),
            "members": members,
        },
    )
    write_jsonl(
        Path("records/_universe-snapshots/_changelog.jsonl"),
        [
            {
                "event_id": "universe-snapshot-20260501",
                "snapshot_path": str(UNIVERSE_SNAPSHOT_PATH),
                "recorded_at": candidates_doc.get("run_at"),
            }
        ],
    )


def migrate_research(
    refs: dict[str, SnapshotRef], candidate_index: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    refs["universe"] = snapshot_ref(UNIVERSE_SNAPSHOT_PATH)
    create_portfolio_exposure_snapshots(refs)
    refs["portfolio_exposure:9682"] = snapshot_ref(PORTFOLIO_EXPOSURE_1330_PATH)
    refs["portfolio_exposure:9692"] = snapshot_ref(PORTFOLIO_EXPOSURE_2000_PATH)
    refs["portfolio_exposure:default"] = snapshot_ref(PORTFOLIO_EXPOSURE_2030_PATH)

    research_meta: dict[str, dict[str, Any]] = {}
    for path in sorted((ROOT / "records/05-research").rglob("*.md")):
        front, body = split_front_matter(path)
        ticker = str(front["ticker"])
        old_decision = str(front.get("decision"))
        playbook_id = str(front.get("playbook"))
        selected_playbooks = [playbook_id, *list(front.get("supporting_signals") or [])]
        selected_refs = []
        candidate_decisions = []
        for selected_playbook in selected_playbooks:
            hit_id = (
                candidate_index.get(ticker, {})
                .get("evidence_by_playbook", {})
                .get(selected_playbook)
            )
            if hit_id is None:
                continue
            selected_refs.append({"source": "candidate", "evidence_hit_id": hit_id})
            effective = ticker != "3678"
            candidate_decisions.append(
                {
                    "evidence_hit_id": hit_id,
                    "effective_sizing_eligible": effective,
                    "evaluated_at": front.get("published_at"),
                    "reason_code": (
                        "corporate_action_post_snapshot" if not effective else "source_status_ok"
                    ),
                }
            )
        independent_count = len(
            {
                PLAYBOOK_COMPONENT.get(playbook, playbook)
                for playbook in selected_playbooks
                if ticker != "3678"
            }
        )
        family_union = sorted(
            {
                family
                for playbook in selected_playbooks
                if ticker != "3678"
                for family in PLAYBOOK_FAMILY.get(playbook, ["valuation"])
            }
        )
        research_decision = _research_decision(old_decision, ticker)
        payoff = _payoff(ticker)
        position_size_oku = front.get("position_size_oku", 0)
        front_new: dict[str, Any] = {
            "ticker": front["ticker"],
            "name": front["name"],
            "playbook_id": playbook_id,
            "playbook_snapshot": refs[f"playbook:{playbook_id}"].as_dict(),
            "policy_snapshot": refs["policy"].as_dict(),
            "portfolio_exposure_snapshot_ref": _portfolio_exposure_ref(refs, ticker).as_dict(),
            "selected_supporting_evidence_refs": selected_refs,
            "research_decision": research_decision,
            "candidates_ref": front["candidates_ref"],
            "candidate_ref": {
                "candidates_ref": front["candidates_ref"],
                "screen_run_id": "screening-20260501-79ec46a8",
                "ticker": ticker,
                "candidate_id": candidate_index.get(ticker, {}).get("candidate_id"),
            },
            "outlook_ref": front["outlook_ref"],
            "brief_refs": front["brief_refs"],
            "ai_draft": front.get("ai-draft", front.get("ai_draft", False)),
            "published_at": front["published_at"],
            "recorded_at": front["published_at"],
            "tradable_at": front["tradable_at"],
            "macro_regime_gate": _macro_regime_gate(front.get("macro_gate")),
            "policy_overrides": _policy_overrides(front.get("overrides", [])),
            "external_refs": front.get("external_refs", []),
            "candidate_evidence_decisions": candidate_decisions,
            "research_evidence_hits": _research_evidence_hits(ticker, front),
            "independent_evidence_count": independent_count,
            "raw_playbook_concurrence_count": len(selected_playbooks),
            "sizing_eligible_playbook_concurrence_count": independent_count,
            "raw_evidence_family_count": len(
                {
                    family
                    for playbook in selected_playbooks
                    for family in PLAYBOOK_FAMILY.get(playbook, ["valuation"])
                }
            ),
            "sizing_eligible_evidence_family_count": len(family_union),
            "conviction_tier": _conviction_tier(old_decision, independent_count),
            "conviction_tier_path": "count_breadth",
            "depth_verification_ref": None,
            "position_sizing_overlay": {
                "paper_proxy_position_size_oku": position_size_oku,
                "paper_proxy_position_size_yen": int(float(position_size_oku or 0) * 100_000_000),
                "real_order_intent_yen": _real_order_intent_yen(ticker),
                "adv_participation_pct": front.get("adv_participation_pct"),
                "sizing_formula_id": "policy-v1-paper-to-real-ladder",
            },
            "counterfactual": _counterfactual(front),
            "thesis_payoff": payoff,
            "tracking": _tracking_for_decision(research_decision),
            "market_cap_oku": front.get("market_cap_oku"),
            "sector_33": front.get("sector_33"),
            "avg_turnover_oku": front.get("avg_turnover_oku"),
            "valuation": front.get("valuation"),
        }
        body = rewrite_body(body)
        write_text(path.relative_to(ROOT), render_front_matter(front_new, body))
        research_meta[ticker] = {
            "research_ref": str(path.relative_to(ROOT)),
            "decision_event_id": f"decision-20260505-{ticker}-research",
            "research_decision": research_decision,
            "playbook_id": playbook_id,
            "published_at": front["published_at"],
            "independent_evidence_count": independent_count,
            "conviction_tier": front_new["conviction_tier"],
        }
    return research_meta


def migrate_trades(refs: dict[str, SnapshotRef]) -> None:
    for path in sorted((ROOT / "records/06-trades").rglob("*.md")):
        front, body = split_front_matter(path)
        ticker = str(front["ticker"])
        order_intent_id = f"intent-20260505-{ticker}-buy"
        order_id = f"order-20260505-{ticker}-buy-1"
        exposure_ref = _portfolio_exposure_ref(refs, ticker)
        guarded_notional = front.get("guarded_max_notional_yen") or front.get(
            "real_order_notional_yen"
        )
        new_front = {
            "trade_id": f"trade-20260505-{ticker}",
            "ticker": front["ticker"],
            "name": front["name"],
            "research_ref": front["research_ref"],
            "policy_snapshot": refs["policy"].as_dict(),
            "portfolio_exposure_snapshot_ref": exposure_ref.as_dict(),
            "position_state": "none",
            "review_state": "not_due",
            "trade_execution_state": "submitted",
            "order_intent": {
                "order_intent_id": order_intent_id,
                "decision_event_id": f"decision-20260505-{ticker}-trade",
                "side": "buy",
                "quantity": front.get("order_quantity"),
                "order_price_guard_yen": front.get("order_price_guard_yen"),
                "expires_at": front.get("expected_fill_at"),
            },
            "orders": [
                {
                    "order_id": order_id,
                    "origin_order_intent_id": order_intent_id,
                    "external_broker_order_id": None,
                    "side": "buy",
                    "state": "submitted",
                    "submitted_quantity": front.get("order_quantity"),
                    "filled_quantity": 0,
                    "order_price_guard_yen": front.get("order_price_guard_yen"),
                    "events": [
                        {
                            "event_id": f"{order_id}-submit",
                            "event_type": "submit",
                            "at": front.get("order_date"),
                            "quantity": front.get("order_quantity"),
                            "price_yen": front.get("order_price_guard_yen"),
                        }
                    ],
                }
            ],
            "executions": [],
            "capital_basis": {
                "real_capital_yen": front.get("real_capital_yen"),
                "tactical_real_budget_yen": front.get("tactical_capital_yen"),
                "paper_proxy_capital_yen": 100_000_000,
            },
            "position_sizing_overlay": {
                "paper_proxy_position_size_oku": front.get("paper_proxy_position_size_oku"),
                "paper_proxy_position_size_pct": front.get("paper_proxy_position_size_pct"),
                "estimated_real_order_notional_yen": front.get("real_order_notional_yen"),
                "guarded_max_notional_yen": guarded_notional,
                "guarded_max_real_concentration_pct": front.get(
                    "guarded_max_real_concentration_pct"
                ),
                "guarded_max_tactical_concentration_pct": front.get(
                    "guarded_max_tactical_concentration_pct"
                ),
            },
            "planned_exit": front.get("planned_exit"),
            "kill_switch_check": front.get("kill_switch_check"),
            "accounting": {"cost_basis_method": "specific_lot"},
            "execution_costs": {
                "commission_yen": None,
                "tax_yen": None,
                "slippage_yen": None,
            },
        }
        write_text(path.relative_to(ROOT), render_front_matter(new_front, rewrite_body(body)))


def migrate_ledger(
    refs: dict[str, SnapshotRef],
    research_meta: dict[str, dict[str, Any]],
    candidate_index: dict[str, dict[str, Any]],
) -> None:
    rows: list[dict[str, Any]] = []
    source_rows: list[tuple[str, dict[str, Any]]] = []
    for ledger_path in [
        Path("records/_ledger/paper/2026-05.jsonl"),
        Path("records/_ledger/skipped/2026-05.jsonl"),
    ]:
        full = ROOT / ledger_path
        if not full.exists():
            continue
        for line in full.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            source_rows.append((str(ledger_path), json.loads(line)))
    for source_path, old in source_rows:
        ticker = str(old["ticker"])
        meta = research_meta.get(ticker, {})
        candidate = candidate_index.get(ticker, {})
        decision_event_id = meta.get("decision_event_id", f"decision-20260505-{ticker}-research")
        rows.append(
            {
                "decision_event_id": decision_event_id,
                "event_kind": "decision",
                "decision_scope": "research_memo",
                "ticker": ticker,
                "name": old.get("name"),
                "candidate_ref": {
                    "candidates_ref": old.get("candidates_ref"),
                    "screen_run_id": "screening-20260501-79ec46a8",
                    "ticker": ticker,
                    "candidate_id": candidate.get("candidate_id"),
                },
                "research_ref": old.get("research_ref"),
                "trade_ref": None,
                "playbook_id": old.get("playbook"),
                "playbook_snapshot": refs[f"playbook:{old.get('playbook')}"].as_dict(),
                "policy_snapshot": refs["policy"].as_dict(),
                "decision_event_at": f"{old.get('decision_date')}T00:00:00+09:00",
                "research_decision": meta.get("research_decision"),
                "candidate_decision": _candidate_decision_from_research(
                    meta.get("research_decision")
                ),
                "trade_execution_state": "none",
                "baseline_price": old.get("baseline_price"),
                "market_cap_oku": old.get("market_cap_oku"),
                "avg_turnover_oku": old.get("avg_turnover_oku"),
                "independent_evidence_count": meta.get("independent_evidence_count"),
                "conviction_tier": meta.get("conviction_tier"),
                "tracking": {
                    "mode": _tracking_mode(meta.get("research_decision")),
                    "plus_15bd": old.get("tracking", {}).get("plus_15bd"),
                    "plus_30bd": old.get("tracking", {}).get("plus_30bd"),
                },
                "migration_source": {
                    "old_path": source_path,
                    "old_ledger_id": old.get("ledger_id"),
                    "old_decision": old.get("decision"),
                },
            }
        )
    for ticker in sorted(candidate_index):
        if ticker in research_meta:
            continue
        candidate = candidate_index[ticker]
        if not candidate.get("evidence_hit_ids"):
            continue
        rows.append(
            {
                "decision_event_id": f"decision-20260505-{ticker}-candidate-not-reviewed",
                "event_kind": "decision",
                "decision_scope": "candidate_screen",
                "ticker": ticker,
                "name": candidate["candidate"].get("name"),
                "candidate_ref": {
                    "candidates_ref": candidate["candidates_ref"],
                    "screen_run_id": "screening-20260501-79ec46a8",
                    "ticker": ticker,
                    "candidate_id": candidate["candidate_id"],
                },
                "research_ref": None,
                "trade_ref": None,
                "candidate_decision": "not_reviewed",
                "not_reviewed_reason": "review_capacity",
                "trade_execution_state": "none",
                "tracking": {"mode": "missed_opportunity_scan"},
            }
        )
    for ticker in ("9682", "9692"):
        meta = research_meta[ticker]
        rows.append(
            {
                "decision_event_id": f"decision-20260505-{ticker}-trade",
                "event_kind": "decision",
                "decision_scope": "trade_execution",
                "ticker": ticker,
                "research_ref": meta["research_ref"],
                "trade_ref": f"records/06-trades/2026/05/2026-05-05-{ticker}.md",
                "order_intent": {
                    "order_intent_id": f"intent-20260505-{ticker}-buy",
                    "side": "buy",
                    "quantity": 200 if ticker == "9682" else 100,
                    "order_price_guard_yen": 1050 if ticker == "9682" else 2000,
                },
                "trade_execution_state": "submitted",
                "policy_snapshot": refs["policy"].as_dict(),
            }
        )
    write_jsonl(Path("records/_ledger/research-decisions/2026-05.jsonl"), rows)
    shutil.rmtree(ROOT / "records/_ledger/paper", ignore_errors=True)
    shutil.rmtree(ROOT / "records/_ledger/skipped", ignore_errors=True)


def create_review_artifacts() -> None:
    write_yaml(
        Path("records/07-reviews/missed-opportunity-scan/2026-05.yaml"),
        {
            "scan_id": "missed-opportunity-2026-05",
            "scan_month": "2026-05",
            "market_data_snapshot_ref": {
                "ref_path": str(MARKET_DATA_PATH),
                "content_sha256": content_sha(MARKET_DATA_PATH),
            },
            "items": [],
            "thresholds": {
                "forward_return_pct": 20.0,
                "forward_max_drawup_pct": 30.0,
                "primary_relative_return_pct": 5.0,
            },
        },
    )
    write_yaml(
        Path("records/07-reviews/screening-false-negative-scan/2026-05.yaml"),
        {
            "scan_id": "screening-false-negative-2026-05",
            "scan_month": "2026-05",
            "start_price_basis": "candidate_run_close_adjusted_close",
            "items": [],
        },
    )
    write_yaml(
        Path("records/07-reviews/universe-attribution/2026-05.yaml"),
        {
            "scan_id": "universe-attribution-2026-05",
            "scan_month": "2026-05",
            "universe_snapshot_ref": {
                "ref_path": str(UNIVERSE_SNAPSHOT_PATH),
                "content_sha256": content_sha(UNIVERSE_SNAPSHOT_PATH),
            },
            "summary": {
                "members_recorded": len(
                    (yaml_load(UNIVERSE_SNAPSHOT_PATH) or {}).get("members", [])
                ),
                "no_hit_false_negative_count": 0,
            },
        },
    )
    write_yaml(
        Path("records/07-reviews/playbook-attribution/2026-05.yaml"),
        {
            "review_id": "playbook-attribution-2026-05",
            "review_month": "2026-05",
            "hit_rate_by_horizon": {},
            "forward_return_distribution": {},
            "drawdown_distribution": {},
            "conviction_tier_breakdown": {},
            "evidence_family_breakdown": {},
            "source_status_breakdown": {},
        },
    )


def create_migration_manifest(refs: dict[str, SnapshotRef]) -> None:
    rows = [
        {
            "migration_run_id": "domain-model-103-20260506",
            "migration_strategy": "rewrite",
            "granularity": "repository",
            "target_payload_tree_sha": "computed-excluding-records/_migrations",
            "derived_fields": [
                {
                    "target_field": "independent_evidence_count",
                    "derivation_kind": "deterministic",
                    "formula_id": "distinct-independence-component-id-v1",
                    "input_refs": [refs["metric_catalog"].as_dict()],
                },
                {
                    "target_field": "position_sizing_overlay.real_order_intent_yen",
                    "derivation_kind": "deterministic",
                    "formula_id": "paper-to-real-order-notional-v1",
                    "input_refs": [refs["policy"].as_dict()],
                },
            ],
            "manual_judgments": [
                {
                    "manual_judgment_id": "mj-3678-corporate-action-post-snapshot",
                    "target_record": (
                        "records/05-research/2026/05/2026-05-05-3678-strict-net-cash-discount.md"
                    ),
                    "reason_code": "corporate_action_post_snapshot",
                }
            ],
        }
    ]
    manifest_path = Path("records/_migrations/2026-05-06-domain-model.jsonl")
    write_jsonl(manifest_path, rows)
    digest = hashlib.sha256((ROOT / manifest_path).read_bytes()).hexdigest()
    write_text(Path("records/_migrations/2026-05-06-domain-model.jsonl.sha256"), f"{digest}\n")


def create_portfolio_exposure_snapshots(refs: dict[str, SnapshotRef]) -> None:
    base = {
        "source_decision_register_refs": [],
        "source_trade_refs": [],
        "source_broker_snapshot_refs": [],
        "positions": [],
        "exposures": [],
    }
    write_yaml(
        PORTFOLIO_EXPOSURE_1330_PATH,
        {
            "snapshot_id": "portfolio-exposure-20260505-133000",
            "as_of": "2026-05-05T13:30:00+09:00",
            **base,
            "outstanding_orders": [],
            "remaining_tactical_real_budget_yen": 1000000,
        },
    )
    write_yaml(
        PORTFOLIO_EXPOSURE_2000_PATH,
        {
            "snapshot_id": "portfolio-exposure-20260505-200000",
            "as_of": "2026-05-05T20:00:00+09:00",
            **base,
            "outstanding_orders": [
                {
                    "origin_order_intent_id": "intent-20260505-9682-buy",
                    "ticker": "9682",
                    "side": "buy",
                    "guarded_notional_yen": 210000,
                    "playbook_id": "sales-discount-growth",
                    "economic_exposure_group": "sales-discount-growth",
                }
            ],
            "remaining_tactical_real_budget_yen": 790000,
        },
    )
    write_yaml(
        PORTFOLIO_EXPOSURE_2030_PATH,
        {
            "snapshot_id": "portfolio-exposure-20260505-203000",
            "as_of": "2026-05-05T20:30:00+09:00",
            **base,
            "outstanding_orders": [
                {
                    "origin_order_intent_id": "intent-20260505-9682-buy",
                    "ticker": "9682",
                    "side": "buy",
                    "guarded_notional_yen": 210000,
                    "playbook_id": "sales-discount-growth",
                    "economic_exposure_group": "sales-discount-growth",
                },
                {
                    "origin_order_intent_id": "intent-20260505-9692-buy",
                    "ticker": "9692",
                    "side": "buy",
                    "guarded_notional_yen": 200000,
                    "playbook_id": "sales-discount-growth",
                    "economic_exposure_group": "sales-discount-growth",
                },
            ],
            "remaining_tactical_real_budget_yen": 590000,
        },
    )
    write_yaml(
        MARKET_DATA_PATH,
        {
            "snapshot_id": "market-data-20260501-close",
            "as_of": "2026-05-01",
            "price_basis": "adjusted_close",
            "prices": {
                "3678": 1225,
                "6310": 1726,
                "6835": 262,
                "7613": 1284,
                "9682": 1014,
                "9692": 1929,
            },
        },
    )


def split_front_matter(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
    if not match:
        raise RuntimeError(f"missing front matter: {path}")
    front = yaml.safe_load(match.group(1))
    if not isinstance(front, dict):
        raise RuntimeError(f"front matter is not mapping: {path}")
    return front, match.group(2)


def render_front_matter(front: dict[str, Any], body: str) -> str:
    yaml_text = yaml.safe_dump(front, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"---\n{yaml_text}---\n{body}"


def rewrite_body(body: str) -> str:
    replacements = {
        "4 成分アーキテクチャ": "decision loop",
        "**Playbook**": "**Playbook id**",
        "**Macro gate**": "**Macro regime gate**",
        "## Macro gate": "## Macro regime gate",
        "Macro gate": "Macro regime gate",
        "macro gate": "macro regime gate",
        "single signal": "single evidence hit",
        "single `sales-discount-growth`": "single `sales-discount-growth` evidence hit",
        "4 signal": "4 evidence hits",
        "signal 数": "evidence hit count",
        "supporting signals": "supporting evidence hits",
        "primary signal": "primary evidence hit",
        "single evidence_hit": "single evidence hit",
        "decision: accepted": "research_decision: approved",
        "decision: pending": "research_decision: deferred",
        "decision: skipped": "research_decision: rejected",
        "skipped のため": "rejected のため",
        "**decision**: accepted": "**research_decision**: approved",
        "**decision**: skipped": "**research_decision**: rejected",
        "accepted thesis": "approved thesis",
        "accepted": "approved",
        "pending": "deferred",
        "skipped": "rejected",
    }
    for old, new in replacements.items():
        body = body.replace(old, new)
    return body


def _research_decision(old_decision: str, ticker: str) -> dict[str, Any]:
    if old_decision == "accepted":
        return {
            "outcome": "approved",
            "posture": "act_now",
            "reason_code": "payoff_and_policy_pass",
        }
    if old_decision == "pending":
        return {
            "outcome": "passed",
            "posture": "wait_for_event",
            "deferral_reason": "event_pending",
            "revisit": {
                "trigger": "earnings_release" if ticker in {"6310", "6835"} else "thesis_update",
                "revisit_after": "2026-05-15" if ticker in {"6310", "6835"} else "2026-05-12",
                "expires_at": "2026-06-30",
                "blocking_conditions": ["event risk must clear"],
            },
        }
    return {
        "outcome": "rejected",
        "posture": "dropped",
        "rejection_reason": (
            "corporate_action_post_snapshot" if ticker == "3678" else "underwriting_fail"
        ),
    }


def _policy_overrides(overrides: Any) -> list[dict[str, Any]]:
    converted = []
    for override in overrides or []:
        if not isinstance(override, dict):
            continue
        converted.append(
            {
                "override_id": f"override-{len(converted) + 1}",
                "type": override.get("type"),
                "prior_state_ref": override.get("prior_state_ref"),
                "prior_state": override.get("prior_state"),
                "new_state": override.get("new_state"),
                "reason": override.get("reason"),
            }
        )
    return converted


def _macro_regime_gate(old_gate: Any) -> dict[str, Any]:
    mapping = {
        "tailwind": ("supportive", "pass"),
        "neutral": ("neutral", "conditional"),
        "headwind": ("adverse", "block"),
    }
    aggregate, effect = mapping.get(str(old_gate), ("unknown", "conditional"))
    return {
        "aggregate_status": aggregate,
        "decision_effect": effect,
        "source_scope": "sector",
        "reducer_id": "macro-regime-reducer-v1",
        "inputs": [],
    }


def _research_evidence_hits(ticker: str, front: dict[str, Any]) -> list[dict[str, Any]]:
    if ticker == "3678":
        return [
            {
                "evidence_hit_id": "research-3678-seven-seas-post-snapshot",
                "decision_role": "freshness_adjustment",
                "evidence_polarity": "contradicts",
                "evidence_family_set": ["fundamental"],
                "source_status": "ok",
                "analyst_asserted": True,
                "sizing_eligible": False,
                "source_refs": [
                    "https://mediado.jp/corporate/15057/",
                    "https://mediado.jp/medicome/challenge/15992/",
                ],
                "recorded_at": front.get("published_at"),
            }
        ]
    hits = []
    if ticker in {"9682", "9692"}:
        hits.append(
            {
                "evidence_hit_id": f"research-{ticker}-shareholder-return",
                "decision_role": "catalyst_note",
                "evidence_polarity": "supports",
                "evidence_family_set": ["catalyst"],
                "source_status": "ok",
                "analyst_asserted": True,
                "sizing_eligible": False,
                "source_refs": front.get("external_refs", []),
                "recorded_at": front.get("published_at"),
            }
        )
    hits.append(
        {
            "evidence_hit_id": f"research-{ticker}-risk-review",
            "decision_role": "risk_evidence",
            "evidence_polarity": "risk",
            "evidence_family_set": ["fundamental"],
            "source_status": "ok",
            "analyst_asserted": True,
            "sizing_eligible": False,
            "source_refs": front.get("external_refs", []),
            "recorded_at": front.get("published_at"),
        }
    )
    return hits


def _payoff(ticker: str) -> dict[str, Any]:
    payoff = dict(RESEARCH_PAYOFF[ticker])
    entry = float(payoff["max_entry_price_yen"])
    target = float(payoff["target_price_yen"])
    stop = float(payoff["stop_loss_yen"])
    upside = round((target / entry - 1) * 100, 2)
    downside = round((entry / stop - 1) * 100, 2)
    payoff["entry_trigger"] = "price_guard_or_revisit"
    payoff["expected_upside_pct"] = upside
    payoff["expected_downside_pct"] = downside
    payoff["risk_reward_ratio"] = round(upside / downside, 2) if downside else None
    return payoff


def _counterfactual(front: dict[str, Any]) -> dict[str, Any] | None:
    if "hypothetical_position_size_oku" not in front:
        return None
    return {
        "if_approved": {
            "paper_proxy_position_size_oku": front.get("hypothetical_position_size_oku")
        }
    }


def _tracking_for_decision(research_decision: dict[str, Any]) -> dict[str, Any]:
    if research_decision["outcome"] == "approved":
        return {"mode": "post_approval", "plus_15bd": None, "plus_30bd": None}
    if research_decision["outcome"] == "passed":
        return {"mode": "re_examination", "plus_15bd": None, "plus_30bd": None}
    return {"mode": "missed_opportunity_scan", "plus_15bd": None, "plus_30bd": None}


def _portfolio_exposure_ref(refs: dict[str, SnapshotRef], ticker: str) -> SnapshotRef:
    if ticker == "9682":
        return refs["portfolio_exposure:9682"]
    if ticker == "9692":
        return refs["portfolio_exposure:9692"]
    return refs["portfolio_exposure:default"]


def _real_order_intent_yen(ticker: str) -> int | None:
    return {"9682": 210000, "9692": 200000}.get(ticker)


def _conviction_tier(old_decision: str, independent_count: int) -> str:
    if old_decision == "skipped":
        return "blocked"
    if independent_count >= 3:
        return "high"
    if independent_count >= 1:
        return "medium"
    return "low"


def _candidate_decision_from_research(research_decision: Any) -> str:
    if not isinstance(research_decision, dict):
        return "not_reviewed"
    match research_decision.get("outcome"):
        case "approved":
            return "selected"
        case "passed":
            return "deferred"
        case "rejected":
            return "rejected"
    return "not_reviewed"


def _tracking_mode(research_decision: Any) -> str:
    if not isinstance(research_decision, dict):
        return "none"
    match research_decision.get("outcome"):
        case "approved":
            return "post_approval"
        case "passed":
            return "re_examination"
        case "rejected":
            return "missed_opportunity_scan"
    return "none"


def _security_exposures(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    sector = str(candidate.get("sector_33", ""))
    if sector == "情報・通信業":
        bucket = "global-tech-cycle"
    elif sector in {"小売業", "水産・農林業"}:
        bucket = "japan-domestic-demand"
    elif sector in {"機械", "電気機器", "卸売業"}:
        bucket = "japan-external-demand"
    else:
        bucket = "japan-domestic-demand"
    return [
        {
            "exposure_bucket": bucket,
            "weight_or_materiality": "medium",
            "source_refs": [],
            "confidence": "medium",
        }
    ]


def _source_snapshot_group(playbook_id: str) -> str:
    if playbook_id in {"strict-net-cash-discount", "cash-rich-asset-discount"}:
        return "balance-sheet"
    if playbook_id in {"cashflow-yield-discount", "fcf-yield-discount"}:
        return "cashflow-statement"
    if playbook_id == "valuation-reversion":
        return "market-price"
    return "market-and-sales"


def _freshness_dependency(playbook_id: str) -> str:
    if playbook_id == "valuation-reversion":
        return "market_data"
    if playbook_id in {"strict-net-cash-discount", "cash-rich-asset-discount"}:
        return "balance_sheet"
    return "financial_statement"


def _metric_snapshot_at(candidate: dict[str, Any]) -> Any:
    metrics = candidate.get("metrics")
    if isinstance(metrics, dict):
        return metrics.get("edinet_source_submit_datetime")
    return None


def _hash_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    main()
