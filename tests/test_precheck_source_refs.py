from __future__ import annotations

from pathlib import Path

import yaml

from baibai_loop.precheck.source_refs import scan_outlook_source_refs


def _write_brief(path: Path, sources: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "kind": "world-weekly",
        "sources": [{"id": k, "name": k, "url": "https://example.com", **sources[k]} for k in []],
        "summary": "\n".join(sources.values()),
    }
    path.write_text(yaml.safe_dump(payload, allow_unicode=True))


def _write_outlook(path: Path, *, sectors: dict[str, dict[str, object]] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "schema_version": 1,
        "horizon": "1-6m",
        "sectors": sectors or {},
        "regions": {},
        "changes": [],
    }
    path.write_text(yaml.safe_dump(payload, allow_unicode=True))


def _setup_repo(tmp_path: Path, *, brief_text: str, rationale_text: str) -> Path:
    """Create minimal repo layout for one outlook + one brief."""
    brief_path = tmp_path / "records/01-brief/2026/04/2026-04-26-world-weekly-test.yaml"
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    brief_path.write_text(brief_text, encoding="utf-8")
    outlook_path = tmp_path / "records/02-outlook/2026/05/outlook-2026-05-04-test.yaml"
    _write_outlook(
        outlook_path,
        sectors={
            "情報・通信業": {
                "status": "tailwind",
                "rationale": rationale_text,
                "source_refs": [
                    "records/01-brief/2026/04/2026-04-26-world-weekly-test.yaml",
                ],
            },
        },
    )
    return tmp_path / "records/02-outlook"


def test_token_present_in_brief_passes(tmp_path: Path) -> None:
    outlook_root = _setup_repo(
        tmp_path,
        brief_text="春闘 5.09% で 3 年連続 5% 超",
        rationale_text="春闘 5.09% による人件費上昇は要観察",
    )
    findings = scan_outlook_source_refs(outlook_root, repo_root=tmp_path)
    assert findings == []


def test_token_missing_from_brief_is_flagged(tmp_path: Path) -> None:
    outlook_root = _setup_repo(
        tmp_path,
        brief_text="CPI 1.5% / 小売 +2.5%",  # 春闘 / 5.09 は含まれない
        rationale_text="春闘 5.09% による人件費上昇は要観察",
    )
    findings = scan_outlook_source_refs(outlook_root, repo_root=tmp_path)
    codes = {f.code for f in findings}
    assert "precheck.token-not-in-source-refs" in codes
    tokens = {f.token for f in findings}
    assert "5.09%" in tokens


def test_empty_source_refs_with_tokens_is_flagged(tmp_path: Path) -> None:
    outlook_path = tmp_path / "records/02-outlook/2026/05/outlook-2026-05-04-test.yaml"
    _write_outlook(
        outlook_path,
        sectors={
            "情報・通信業": {
                "status": "tailwind",
                "rationale": "コア PCE +3.2% YoY",
                "source_refs": [],
            },
        },
    )
    findings = scan_outlook_source_refs(tmp_path / "records/02-outlook", repo_root=tmp_path)
    codes = {f.code for f in findings}
    assert "precheck.source-refs-empty" in codes


def test_empty_source_refs_without_tokens_is_silent(tmp_path: Path) -> None:
    outlook_path = tmp_path / "records/02-outlook/2026/05/outlook-2026-05-04-test.yaml"
    _write_outlook(
        outlook_path,
        sectors={
            "水産・農林業": {
                "status": "neutral",
                "rationale": "個別事象なし",
                "source_refs": [],
            },
        },
    )
    findings = scan_outlook_source_refs(tmp_path / "records/02-outlook", repo_root=tmp_path)
    assert findings == []


def test_token_normalization_handles_inner_whitespace(tmp_path: Path) -> None:
    outlook_root = _setup_repo(
        tmp_path,
        brief_text="春闘 5.09 %",  # internal space variant
        rationale_text="春闘 5.09% による人件費上昇",
    )
    findings = scan_outlook_source_refs(outlook_root, repo_root=tmp_path)
    assert [f for f in findings if "5.09" in f.token] == []


def test_changes_rationale_is_checked(tmp_path: Path) -> None:
    brief_path = tmp_path / "records/01-brief/2026/04/2026-04-26-test.yaml"
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    brief_path.write_text("CPI 1.5%", encoding="utf-8")
    outlook_path = tmp_path / "records/02-outlook/2026/05/outlook-2026-05-04-test.yaml"
    payload = {
        "schema_version": 1,
        "horizon": "1-6m",
        "sectors": {},
        "regions": {},
        "changes": [
            {
                "target": "情報・通信業",
                "from_status": "neutral",
                "to_status": "tailwind",
                "rationale": "春闘 5.09% を反映",
                "source_refs": ["records/01-brief/2026/04/2026-04-26-test.yaml"],
            }
        ],
    }
    outlook_path.parent.mkdir(parents=True, exist_ok=True)
    outlook_path.write_text(yaml.safe_dump(payload, allow_unicode=True))
    findings = scan_outlook_source_refs(tmp_path / "records/02-outlook", repo_root=tmp_path)
    locations = {f.location for f in findings}
    assert "changes[0](情報・通信業).rationale" in locations
