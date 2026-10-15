from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "web/contracts/routes.json"
LOCAL_API_SOURCE = ROOT / "web/backend/src/baibai_web/api/server.py"
MATERIALIZER_SOURCE = ROOT / "web/backend/src/baibai_web/materialize.py"
EDGE_SOURCE = ROOT / "web/edge/src/index.ts"
FRONTEND_SOURCE_ROOT = ROOT / "web/frontend/src"


def _contract() -> dict[str, object]:
    loaded = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_web_route_inventory_is_present_at_both_serving_boundaries() -> None:
    contract = _contract()
    computed = contract["computed_routes"]
    routes = contract["routes"]
    edge_routes = contract["edge_routes"]
    patterns = contract["patterns"]
    assert isinstance(computed, list)
    assert isinstance(routes, dict)
    assert isinstance(edge_routes, dict)
    assert isinstance(patterns, dict)

    local_source = LOCAL_API_SOURCE.read_text(encoding="utf-8")
    edge_source = EDGE_SOURCE.read_text(encoding="utf-8")
    route_templates = [*computed, *routes, *patterns]
    local_routes = {
        route
        for route in re.findall(r'@app\.get\(\s*"([^"?]+)', local_source)
        if route.startswith("/api/")
    }
    expected_local_routes = {template.split("?", maxsplit=1)[0] for template in route_templates}
    assert local_routes == expected_local_routes
    for template in route_templates:
        assert isinstance(template, str)
        route = template.split("?", maxsplit=1)[0]
        assert route in local_routes, f"local API does not expose {template}"
        edge_marker = route.split("{", maxsplit=1)[0]
        assert edge_marker in edge_source, f"edge does not expose {template}"
    for template in edge_routes:
        assert template in edge_source, f"edge does not expose {template}"

    edge_cases = set(re.findall(r"case '(/api[^']+)':", edge_source))
    expected_edge_cases = {
        path
        for route in [*computed, *routes, *edge_routes, *patterns]
        if "{" not in (path := route.split("?", maxsplit=1)[0])
    }
    assert edge_cases == expected_edge_cases


def test_every_contract_object_key_has_a_materializer_marker() -> None:
    contract = _contract()
    routes = contract["routes"]
    edge_routes = contract["edge_routes"]
    patterns = contract["patterns"]
    assert isinstance(routes, dict)
    assert isinstance(edge_routes, dict)
    assert isinstance(patterns, dict)

    materializer_source = MATERIALIZER_SOURCE.read_text(encoding="utf-8")
    for key in [*routes.values(), *edge_routes.values(), *patterns.values()]:
        assert isinstance(key, str)
        filename = key.rsplit("/", maxsplit=1)[-1]
        static_filename = filename.split("{", maxsplit=1)[0]
        materializer_marker = static_filename or key.rsplit("/", maxsplit=1)[0]
        assert materializer_marker in materializer_source, f"no materializer for {key}"


def test_static_edge_routes_map_to_the_exact_contract_keys() -> None:
    contract = _contract()
    routes = contract["routes"]
    edge_routes = contract["edge_routes"]
    assert isinstance(routes, dict)
    assert isinstance(edge_routes, dict)

    edge_source = EDGE_SOURCE.read_text(encoding="utf-8")
    for route, key in {**routes, **edge_routes}.items():
        assert isinstance(route, str)
        assert isinstance(key, str)
        filename = key.removeprefix("views/")
        collections = {
            "/api/stock-assessments": "assessments",
            "/api/investment-proposals": "proposals",
        }
        if route in collections:
            expected = (
                rf"case '{re.escape(route)}':\s+return {{\s*kind: 'research-index',\s*"
                rf"key: '{re.escape(key)}',\s*collection: '{collections[route]}'\s*}}"
            )
        elif key.startswith("views/"):
            expected = rf"case '{re.escape(route)}':\s+return view\('{re.escape(filename)}'\)"
        else:
            expected = (
                rf"case '{re.escape(route)}':[\s\S]*?"
                rf"return {{ kind: 'view', key: '{re.escape(key)}' }}"
            )
        assert re.search(expected, edge_source), f"edge route/key drift: {route} -> {key}"
    assert re.search(
        r"if \(route.kind === 'research-index'\)\s*return researchIndex\(object, route.collection, url\)",
        edge_source,
    )


def test_dynamic_routes_map_to_exact_edge_keys_and_materializer_outputs() -> None:
    contract = _contract()
    patterns = contract["patterns"]
    assert isinstance(patterns, dict)
    edge_source = EDGE_SOURCE.read_text(encoding="utf-8")
    materializer_source = MATERIALIZER_SOURCE.read_text(encoding="utf-8")
    expected = {
        "/api/screening/history/{as_of}": (
            "resolveScreeningHistory",
            "`history/candidate-views/${asOf}.json`",
            'f"{screening_as_of.isoformat()}.json"',
        ),
        "/api/market-context/assessments/{context_id}": (
            "resolveMarketContextAssessment",
            "`market-context-assessment--${contextId}.json`",
            'f"market-context-assessment--{context_id}.json"',
        ),
        "/api/market-context/indicators/{series_id}": (
            "resolveIndicatorSeries",
            "`indicator-series--${seriesId}.json`",
            'f"indicator-series--{series_id}.json"',
        ),
        "/api/stock-assessments/{assessment_id}": (
            "resolveStockAssessment",
            "`stock-assessment--${assessmentId}.json`",
            'f"stock-assessment--{assessment_id}.json"',
        ),
        "/api/investment-proposals/{proposal_id}": (
            "resolveInvestmentProposal",
            "`investment-proposal--${proposalId}.json`",
            'f"investment-proposal--{proposal_id}.json"',
        ),
        "/api/investment-proposals/{proposal_id}/current-preview": (
            "resolveInvestmentProposalPreview",
            "`investment-proposal-preview--${proposalId}.json`",
            'f"investment-proposal-preview--{proposal_id}.json"',
        ),
        "/api/securities/{ticker}": (
            "resolveSecurity",
            "`security--${ticker}.json`",
            'f"security--{ticker}.json"',
        ),
    }
    assert set(patterns) == set(expected)
    expected_contract_keys = {
        "/api/screening/history/{as_of}": "history/candidate-views/{as_of}.json",
        "/api/market-context/assessments/{context_id}": "views/market-context-assessment--{context_id}.json",
        "/api/market-context/indicators/{series_id}": "views/indicator-series--{series_id}.json",
        "/api/stock-assessments/{assessment_id}": "views/stock-assessment--{assessment_id}.json",
        "/api/investment-proposals/{proposal_id}": "views/investment-proposal--{proposal_id}.json",
        "/api/investment-proposals/{proposal_id}/current-preview": "views/investment-proposal-preview--{proposal_id}.json",
        "/api/securities/{ticker}": "views/security--{ticker}.json",
    }
    assert patterns == expected_contract_keys

    for route, (resolver, edge_key, writer_marker) in expected.items():
        start = edge_source.index(f"function {resolver}(")
        next_function = re.search(r"\n(?:async )?function ", edge_source[start + 1 :])
        end = len(edge_source) if next_function is None else start + 1 + next_function.start()
        resolver_source = edge_source[start:end]
        # Local variable names are implementation details; keep the exact object
        # namespace and one interpolated identifier in the routing contract.
        normalized_key = re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", "{identifier}", edge_key)
        normalized_source = re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", "{identifier}", resolver_source)
        assert normalized_key in normalized_source, f"edge dynamic route/key drift: {route}"
        assert writer_marker in materializer_source, f"materializer output drift: {patterns[route]}"


def test_every_frontend_api_route_is_in_the_contract_inventory() -> None:
    contract = _contract()
    inventory = {
        *contract["computed_routes"],
        *contract["routes"],
        *contract["edge_routes"],
        *contract["patterns"],
    }
    frontend_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(FRONTEND_SOURCE_ROOT.rglob("*"))
        if path.suffix in {".ts", ".tsx"}
    )
    frontend_routes = {
        route for route in re.findall(r"[`'](/api[^`']+)[`']", frontend_source) if route != "/api/"
    }
    normalized = {
        route.replace("${value}", "{as_of}")
        .replace("${encodeURIComponent(contextId)}", "{context_id}")
        .replace("${encodeURIComponent(assessmentId)}", "{assessment_id}")
        .replace("${encodeURIComponent(proposalId)}", "{proposal_id}")
        .replace("${encodeURIComponent(ticker)}", "{ticker}")
        .replace("${encodeURIComponent(openSeriesId)}", "{series_id}")
        .replace("${encodeURIComponent(seriesId)}", "{series_id}")
        # cursorは同じ一覧routeの任意queryであり、別の公開pathではない。
        .replace("${cursorQuery}", "")
        for route in frontend_routes
    }
    assert normalized <= inventory, (
        f"frontend routes missing from contract: {normalized - inventory}"
    )
