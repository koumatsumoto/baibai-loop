from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi.routing import APIRoute

from baibai_web.api.server import create_app

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "web/contracts/routes.json"
EDGE_SOURCE = ROOT / "web/edge/src/index.ts"
FRONTEND_SOURCE_ROOT = ROOT / "web/frontend/src"


def _contract() -> dict[str, object]:
    loaded = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_local_api_route_metadata_matches_the_contract(app_method_root: Path) -> None:
    contract = _contract()
    expected_routes = {
        template.split("?", maxsplit=1)[0]
        for template in [
            *contract["computed_routes"],
            *contract["routes"],
            *contract["patterns"],
        ]
    }
    app = create_app(app_method_root)
    api_routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and (route.path == "/api" or route.path.startswith("/api/"))
    ]

    assert {route.path for route in api_routes} == expected_routes
    for route in api_routes:
        assert route.methods == {"GET"}, f"{route.path} must remain read-only"


def test_worker_static_route_inventory_matches_the_contract() -> None:
    contract = _contract()
    edge_source = EDGE_SOURCE.read_text(encoding="utf-8")
    # Workerにはroute登録metadataがない。未宣言のstatic routeを検出する検査だけを
    # 残し、実際のroute/key対応はedge testsのhandleRequestで確認する。
    edge_cases = set(re.findall(r"case '(/api[^']+)':", edge_source))
    expected_edge_cases = {
        path
        for route in [
            *contract["computed_routes"],
            *contract["routes"],
            *contract["edge_routes"],
            *contract["patterns"],
        ]
        if "{" not in (path := route.split("?", maxsplit=1)[0])
    }
    assert edge_cases == expected_edge_cases


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
