"""合成文型のみ。実記事の本文・概要はfixtureへ転載しない。"""

import hashlib
from datetime import UTC, date, datetime

from baibai_engine.machine_analysis.generation.runners.trend_inputs import TrendGenerationInputs
from baibai_engine.machine_analysis.observations.trends.contracts import (
    SelectedArtifact,
    TrendInputManifest,
    project_text,
)
from baibai_engine.machine_analysis.observations.trends.inputs import (
    FixedTrendInputs,
    IndustryInput,
    IndustryScope,
    TrendCoverage,
)

NOW = datetime(2026, 10, 25, tzinfo=UTC)
PHARMA = """
合成の医薬品卸売業512社の2025年度の売上高合計は、2兆3,400億円。
医薬品卸売業512社の2025年度の損益別は、黒字が400社（前年度410社）で78.1％（前年度80.1％）。
日本産業分類（細分類）の「医薬品・医療用品卸売業」を対象に、2025年度の業績
（2025年4月期～2026年3月期決算）を最新期とし、5期連続で業績が判明した512社を抽出、分析した。
2025年の倒産は99件だった。売上高1,000億円超の大手は9社（構成比1.8％）で、
売上高合計は1兆円、利益金合計は100億円に達し、シェアは売上高の42.7％、利益の61.0％を占めている。
"""
INFORMATION = """
情報サービス業2,222社の2025年度の業績は、売上高が10兆2,345億円（前年度比3.2％増）、当期利益は1兆2,345億円（同4.2％増）。
日本産業分類（中分類）「情報サービス業」の2025年度（2025年4月期～2026年3月期）を最新期とし、
5期連続で売上高・当期利益が比較可能な2,222社を抽出、分析した。
売上高も、300人以上の企業が全体の82.3％と8割を超える。
"""
BOJ = """
2026年9月18日 日本銀行
日本銀行は、本日、政策委員会・金融政策決定会合において、次回金融政策決定会合
までの金融市場調節方針を、以下のとおりとすることを決定した（賛成7反対2）。
無担保コールレート（オーバーナイト物）を、１．７５％程度で推移するよう促す1。
補完貸付制度の基準貸付利率は2.0％とする。
1 新たな金融市場調節方針は、翌営業日（9月24日）から適用する。
2 補完当座預金制度の適用利率は、翌営業日（9月24日）から適用する。
ある委員は無担保コールレート（オーバーナイト物）を2.25％程度で推移するよう促すとする議案を提出し、反対多数で否決された。
"""


def document(text, source="tsr", article_id="synthetic-survey", **changes):
    return project_text(
        text,
        **(
            {
                "article_id": article_id,
                "receipt_id": "synthetic-receipt",
                "source": source,
                "source_url": "https://example.invalid/synthetic",
                "raw_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "published_at": NOW,
                "received_at": NOW,
                "content_scope": "full_text",
            }
            | changes
        ),
    )


def fixed(*artifacts, baseline=None, coverage=None, **manifest_changes):
    selections = tuple(SelectedArtifact(current=a, baseline=baseline) for a in artifacts)
    return FixedTrendInputs(
        manifest=TrendInputManifest(
            **(
                {
                    "release_id": "synthetic",
                    "release_sha256": "a" * 64,
                    "cutoff": NOW,
                    "selection_revision": "synthetic-selection-v1",
                    "scope_revision": "synthetic-scopes-v1",
                    "window_start": date(2026, 7, 1),
                    "window_end": date(2026, 10, 25),
                    "selected": tuple(a.ref for a in artifacts),
                    "baselines": () if baseline is None else (baseline.ref,),
                }
                | manifest_changes
            )
        ),
        artifacts=selections,
        coverage=coverage or TrendCoverage(state="available" if artifacts else "not_collected"),
    )


def generation_inputs(*artifacts, related_tickers=("1001", "1002")):
    return TrendGenerationInputs(
        industries=(
            IndustryInput(
                definition=IndustryScope(
                    scope_id="jp-pharmaceutical-wholesale",
                    label="合成業界",
                    taxonomy="synthetic-v1",
                    geography="JP",
                ),
                inputs=fixed(*artifacts),
            ),
        ),
        social=fixed(),
        industry_links=(("jp-pharmaceutical-wholesale", related_tickers),),
    )
