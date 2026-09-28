"""capital for metrics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from enum import Enum
from math import exp, isclose, log

from baibai_engine.market.bars import JQuantsAdjustmentFactorEvent
from baibai_engine.screening.metrics.periods import (
    _accounting_observation_key,
    _AccountingObservationKey,
    _actual_rows,
    _latest_non_null_row,
)
from baibai_engine.screening.providers.edinet import EdinetMetricRecord
from baibai_engine.screening.providers.jquants import JQuantsDailyBar, JQuantsFinancialSummary


@dataclass(frozen=True, slots=True)
class _CapitalBasisResolution:
    """時価総額に使える、同じ資本状態に属する発行済・自己株式数。"""

    issued: float | None
    treasury: float | None
    shares_ex_treasury: float | None
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class _NormalizedSummaryResult:
    """As-of-basis rows plus the newest point across which share facts cannot carry."""

    summaries: Sequence[JQuantsFinancialSummary]
    capital_basis_barrier: _AccountingObservationKey | None


def _cumulative_adjustment_factor_after(
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    after: date,
    asof_date: date,
    *,
    include_boundary_day: bool = False,
) -> float:
    """`after` より後・asof 以前の bar の adjustment_factor の累積を返す。

    `include_boundary_day` は境界日当日の権利落ちも数える。財務開示行が申告する株式基準は
    期末であって開示日ではないので、開示日当日に権利落ちがあった行は分割前基準のまま公表
    される (store 全数で該当 3 行、いずれも直前開示行と同じ株数=分割前基準。分割後基準の
    例は無い)。開示行の正規化はこれを数える。期末と開示日の間に権利落ちがある行は逆に
    134 行中 103 行が既に分割後基準なので、境界を期末側へ広げると大半を二重換算する。
    """
    factor = 1.0
    for bar in ticker_bars:
        before_start = bar.traded_at < after if include_boundary_day else bar.traded_at <= after
        if before_start or bar.traded_at > asof_date:
            continue
        if bar.adjustment_factor in (None, 0.0, 1.0):
            continue
        assert bar.adjustment_factor is not None
        factor *= bar.adjustment_factor
    return factor


class _ShareBasis(Enum):
    """開示行が申告している株式基準。

    期末と開示日の間に権利落ちがある行は、提出者によって期末基準のままのものと分割を
    遡及適用したものに割れる。どちらかで換算の要否が逆になるので、行ごとに決める。
    """

    AS_OF_PERIOD_END = "as_of_period_end"
    AS_OF_DISCLOSURE = "as_of_disclosure"
    INDETERMINATE = "indeterminate"


def _closer_share_basis(ratio: float, interim: float) -> _ShareBasis:
    """株数比を、分割前基準と分割後基準のどちらに寄せるか。

    2 つの仮説は `1/interim` 倍 (実データで 1.3〜10 倍) 離れており、その間に乗るのは
    実際の増減資である。増減資は普通この間隔よりずっと小さいので、**対数空間で近い方の
    極へ寄せ、どちらの極からも幾何中点より遠いときだけ答えない**。固定幅の帯は極の間隔を
    無視するため、分割と同時に数 % の増資があった行 (7066 は比 2.048 / 期待 2.0) を
    分けられなくなる。
    """

    if ratio <= 0 or interim <= 0:
        return _ShareBasis.INDETERMINATE
    separation = abs(log(1.0 / interim))
    if separation == 0.0:
        return _ShareBasis.INDETERMINATE
    to_period_end = abs(log(ratio))
    to_disclosure = abs(log(ratio * interim))
    nearest = min(to_period_end, to_disclosure)
    # 幾何中点より遠い比は、どちらの極から見ても説明が付かない。分割が小さいほど極が
    # 近いので、この条件だけが効く場面がある (1.3 倍の分割では残差 14% で中点に届く)。
    if nearest > separation / 2.0:
        return _ShareBasis.INDETERMINATE
    # 極に寄せたあとに残る株数変化。実データではここが 12.4% までと 29.3% からに分かれ、
    # 間の 17pt は空である。空白の中央で切り、残差の大きい行は答えない — 分割と同時に
    # 大きな増減資があった行は、どちらの仮説を採っても株数が数倍ずれうる (6628 は
    # 1:5 併合と増資が重なり、比 0.51 が対数上は分割前基準に近く見える)。
    if abs(exp(-nearest) - 1.0) > _SPLIT_RESIDUAL_SHARE_CHANGE_LIMIT:
        return _ShareBasis.INDETERMINATE
    return (
        _ShareBasis.AS_OF_PERIOD_END
        if to_period_end <= to_disclosure
        else _ShareBasis.AS_OF_DISCLOSURE
    )


# 極へ寄せたあとに残ってよい株数変化。実測の空白 (12.4% / 29.3%) の中央に置く。
_SPLIT_RESIDUAL_SHARE_CHANGE_LIMIT = 0.20


def _interim_split_basis(
    summaries: Sequence[JQuantsFinancialSummary],
    index: int,
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
) -> tuple[float, _ShareBasis]:
    """期末と開示日の間の権利落ちの累積と、その行が申告している株式基準。

    権利落ちが無ければ換算は 1.0 で、基準を問う必要も無い。あるときは **権利落ち日以前に
    開示された最新行** の as-reported 株数と比べる。その行は分割より前に公表されているので
    必ず分割前基準にある。比が 1 ならこの行も分割前基準 (=期末基準) のまま公表されており、
    `1/factor` なら提出者が分割を遡及適用している。

    参照を「直前の行」にすると、訂正開示のように同じ値を持つ行が並んだ場合に基準の未確定な
    行と比べることになり、比 1.0 が「前行と同じ」ではなく「基準が同じ」と読めてしまう。
    """

    summary = summaries[index]
    period_end = summary.period_end
    if period_end is None:
        return 1.0, _ShareBasis.AS_OF_DISCLOSURE
    interim = 1.0
    first_ex: date | None = None
    for bar in ticker_bars:
        if not (period_end < bar.traded_at < summary.disclosed_at):
            continue
        if bar.adjustment_factor in (None, 0.0, 1.0):
            continue
        assert bar.adjustment_factor is not None
        interim *= bar.adjustment_factor
        if first_ex is None or bar.traded_at < first_ex:
            first_ex = bar.traded_at
    if interim in (0.0, 1.0) or first_ex is None:
        return 1.0, _ShareBasis.AS_OF_DISCLOSURE

    def classify(field_name: str) -> _ShareBasis:
        current = getattr(summary, field_name)
        previous_row = next(
            (
                row
                for row in reversed(summaries[:index])
                if (value := getattr(row, field_name)) is not None
                and value > 0
                and row.disclosed_at < first_ex
            ),
            None,
        )
        if current is None or current <= 0 or previous_row is None:
            return _ShareBasis.INDETERMINATE
        previous = getattr(previous_row, field_name)
        assert previous is not None
        # 比較元の開示後から現在行の期末までに別の corporate action がある場合、生の株数は
        # 現在行の期末基準ではない。先に同じ基準へ移さないと、連続した分割・併合をすべて
        # 今回の interim action の差と誤認する。開示日当日の権利落ちは開示行の正規化と同じ
        # 契約で数える。
        previous_to_period_end = _cumulative_adjustment_factor_after(
            ticker_bars,
            previous_row.disclosed_at,
            period_end,
            include_boundary_day=True,
        )
        if previous_to_period_end <= 0:
            return _ShareBasis.INDETERMINATE
        previous_on_period_end_basis = previous / previous_to_period_end
        return _closer_share_basis(current / previous_on_period_end_basis, interim)

    issued_basis = classify("shares_outstanding")
    if issued_basis is not _ShareBasis.INDETERMINATE:
        return interim, issued_basis
    # AvgSh は期中平均でありgross issuedの値としては使えないが、同じ開示のper-share値と
    # どちらのsplit basisを共有するかを判定する独立anchorにはなる。確定できる場合だけ
    # classifierを補い、正規化後のcapital値は引き続きShOutFY/TrShFYから作る。
    return interim, classify("average_shares")


def _without_share_basis(summary: JQuantsFinancialSummary) -> JQuantsFinancialSummary:
    """株式基準を決められなかった行から、基準に依存する量を落とす。

    円の総額 (総資産・売上・利益) は基準に依存しないので残す。株数と per-share は
    どちらの基準か分からないまま価格と組むと時価総額が分割比だけずれるので答えない。
    時価総額が出ない銘柄は母集団に入らない。
    """

    return replace(
        summary,
        eps_ttm=None,
        forecast_eps=None,
        bps=None,
        dps_actual_annual=(0.0 if summary.dps_actual_annual == 0 else None),
        dps_forecast_annual=(0.0 if summary.dps_forecast_annual == 0 else None),
        shares_outstanding=None,
        average_shares=None,
        treasury_shares=None,
    )


def _normalize_summaries_to_asof_basis(
    summaries: Sequence[JQuantsFinancialSummary],
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    asof_date: date,
) -> Sequence[JQuantsFinancialSummary]:
    """financial summary 行の per-share 値と株数を asof 時点の株式基準へ換算する。

    J-Quants の財務開示行は as-reported (分割の遡及調整なし) のため、開示後に
    分割・併合 (権利落ち bar の adjustment_factor) があると、行同士の比較・合成
    (TTM 合成・YoY・株数変化) や「開示時点株数 x 権利落ち後価格」の market cap で
    per-share 基準が混在する (1:2 分割なら時価総額が半分・合成 EPS が過大に見える)。
    各行の開示日より後・asof 以前の adjustment_factor を累積し、実績系の
    per-share 値 (eps_ttm / bps) には掛け、株数には割って現在基準へ揃える。
    実績系と株数は同一行内で開示日基準に揃っている (1899/1911 の実データで確認) が、
    forecast_eps だけは「会社が分割考慮後の値で開示する」慣行が混在し、開示時点の
    基準を機械では判別できない。分割を跨ぐ行の forecast_eps は正規化せず None に
    落とす (偽の per_forward を出さない。split_adjustment_recent risk tag が
    research の手動検算へ誘導する)。
    """
    return _normalize_summaries_with_status(summaries, ticker_bars, asof_date).summaries


def _normalize_summaries_with_status(
    summaries: Sequence[JQuantsFinancialSummary],
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    asof_date: date,
) -> _NormalizedSummaryResult:
    """Normalize rows while preserving an explicit fail-close capital carry barrier."""

    has_adjustment = any(
        bar.adjustment_factor not in (None, 0.0, 1.0)
        for bar in ticker_bars
        if bar.traded_at <= asof_date
    )
    if not has_adjustment:
        return _NormalizedSummaryResult(summaries=summaries, capital_basis_barrier=None)
    normalized: list[JQuantsFinancialSummary] = []
    capital_basis_barrier: _AccountingObservationKey | None = None
    for index, summary in enumerate(summaries):
        factor = _cumulative_adjustment_factor_after(
            ticker_bars, summary.disclosed_at, asof_date, include_boundary_day=True
        )
        interim, basis = _interim_split_basis(summaries, index, ticker_bars)
        if basis is _ShareBasis.INDETERMINATE:
            normalized.append(_without_share_basis(summary))
            observation_key = _accounting_observation_key(summary)
            if (summary.shares_outstanding is not None or summary.treasury_shares is not None) and (
                capital_basis_barrier is None or observation_key > capital_basis_barrier
            ):
                capital_basis_barrier = observation_key
            continue
        if basis is _ShareBasis.AS_OF_PERIOD_END:
            # 期末基準のまま公表された行なので、期末と開示日の間の権利落ちも数える。
            factor *= interim
        if factor <= 0 or factor == 1.0:
            normalized.append(summary)
            continue
        normalized.append(
            replace(
                summary,
                eps_ttm=summary.eps_ttm * factor if summary.eps_ttm is not None else None,
                forecast_eps=None,
                # 実績 DPS は per-share 実績と同じ換算。予想 DPS は基準不明の
                # 正値だけ落とし、どの株式基準でも同じ明示 0 は残す。
                dps_actual_annual=(
                    summary.dps_actual_annual * factor
                    if summary.dps_actual_annual is not None
                    else None
                ),
                dps_forecast_annual=(0.0 if summary.dps_forecast_annual == 0 else None),
                bps=summary.bps * factor if summary.bps is not None else None,
                shares_outstanding=(
                    summary.shares_outstanding / factor
                    if summary.shares_outstanding is not None
                    else None
                ),
                # 期中平均株式数も株数なので同じ換算を掛ける。掛けないと、開示より後に
                # 分割のある行で「発行済 - 自己株」との比が factor 倍ずれ、健全性 gate が
                # 本来効かせたい (分割が複数ある) 行でだけ静かに無効になる。
                average_shares=(
                    summary.average_shares / factor if summary.average_shares is not None else None
                ),
                # 自己株式数は発行済と同じ株数なので同じ換算を掛ける。片方だけ換算すると
                # 差である自己株控除後株式数が分割のたびに壊れる。`equity_to_asset_ratio`
                # は比率なので分割で動かず、そのまま持ち越される。
                treasury_shares=(
                    summary.treasury_shares / factor
                    if summary.treasury_shares is not None
                    else None
                ),
            )
        )
    return _NormalizedSummaryResult(
        summaries=normalized,
        capital_basis_barrier=capital_basis_barrier,
    )


def _issued_matches_average_alias(
    summary: JQuantsFinancialSummary,
    treasury: float,
    summaries: Sequence[JQuantsFinancialSummary],
) -> bool:
    """Identify a stored AvgSh fallback only when an earlier gross issue count proves it."""

    issued = summary.shares_outstanding
    average = summary.average_shares
    if issued is None or average is None or treasury <= 0:
        return False
    if not isclose(issued, average, rel_tol=1e-12, abs_tol=1e-6):
        return False
    reconstructed_gross = issued + treasury
    source_key = _accounting_observation_key(summary)
    return any(
        row.shares_outstanding is not None
        and _accounting_observation_key(row) < source_key
        and isclose(
            reconstructed_gross,
            row.shares_outstanding,
            rel_tol=1e-12,
            abs_tol=1e-6,
        )
        for row in summaries
    )


def _resolve_capital_basis(
    summaries: Sequence[JQuantsFinancialSummary],
    *,
    capital_basis_barrier: _AccountingObservationKey | None = None,
) -> _CapitalBasisResolution:
    """発行済と自己株式を、両方が成立する資本状態でだけ組み合わせる。

    自己株式0株がJ-Quantsで欠損値になる行があるため、正の自己株式を観測した後に発行済株式を
    持つ新しい行が自己株式を欠く場合、旧自己株式が現在状態でも有効か判別できない。
    自己株式の消却、処分、新株発行と処分の同時実施を区別できないので、新しい自己株式を
    観測するまで旧正値をcarryせずfail closedにする。自己株式0株は二重控除を起こさない
    ため、明示的な0観測は通常どおりcarryできる。

    `AvgSh` は EPS の期中平均株式数であり、発行済株式総数ではない。発行済と期中平均が
    同値で、さらに自己株式を足すと過去に観測した gross issued へ戻る場合、入力の発行済欄へ
    期中平均がfallbackしたと識別できるので答えない。単なる同値は1Qの正常行にもあるため
    failureにはしない。ingestも`ShOutFY`だけを発行済株式総数として保存する。
    """

    actual_rows = _actual_rows(summaries)
    capital_rows = (
        [row for row in actual_rows if _accounting_observation_key(row) > capital_basis_barrier]
        if capital_basis_barrier is not None
        else actual_rows
    )
    issued_row = _latest_non_null_row(capital_rows, "shares_outstanding")
    treasury_row = _latest_non_null_row(capital_rows, "treasury_shares")
    issued = issued_row.shares_outstanding if issued_row is not None else None
    treasury = treasury_row.treasury_shares if treasury_row is not None else None
    if issued is None or treasury is None:
        return _CapitalBasisResolution(
            issued=issued,
            treasury=treasury,
            shares_ex_treasury=None,
            failure_reason=(
                "indeterminate_share_basis" if capital_basis_barrier is not None else None
            ),
        )
    if issued <= 0 or treasury < 0 or issued <= treasury:
        return _CapitalBasisResolution(
            issued=issued,
            treasury=treasury,
            shares_ex_treasury=None,
            failure_reason="invalid_issued_or_treasury_shares",
        )

    if issued_row is not None and treasury_row is not None:
        issued_source_key = _accounting_observation_key(issued_row)
        treasury_source_key = _accounting_observation_key(treasury_row)
        if issued_source_key != treasury_source_key and treasury > 0:
            issued_at_treasury = treasury_row.shares_outstanding
            if issued_at_treasury is None:
                return _CapitalBasisResolution(
                    issued=issued,
                    treasury=treasury,
                    shares_ex_treasury=None,
                    failure_reason="treasury_observation_without_issued_basis",
                )
            if issued_at_treasury <= 0 or issued_at_treasury <= treasury:
                return _CapitalBasisResolution(
                    issued=issued,
                    treasury=treasury,
                    shares_ex_treasury=None,
                    failure_reason="invalid_treasury_source_capital_basis",
                )
            if _accounting_observation_key(issued_row) > _accounting_observation_key(treasury_row):
                return _CapitalBasisResolution(
                    issued=issued,
                    treasury=treasury,
                    shares_ex_treasury=None,
                    failure_reason=(
                        "indeterminate_positive_treasury_after_later_issued_observation"
                    ),
                )

        if _issued_matches_average_alias(issued_row, treasury, capital_rows):
            return _CapitalBasisResolution(
                issued=issued,
                treasury=treasury,
                shares_ex_treasury=None,
                failure_reason="issued_matches_average_with_positive_treasury",
            )

    return _CapitalBasisResolution(
        issued=issued,
        treasury=treasury,
        shares_ex_treasury=_shares_excluding_treasury(issued, treasury),
        failure_reason=None,
    )


def build_shares_outstanding_index(
    summaries_by_ticker: Mapping[str, Sequence[JQuantsFinancialSummary]],
    bars_by_ticker: Mapping[str, Sequence[JQuantsDailyBar]],
    asof_date: date,
    *,
    adjustment_events_by_ticker: Mapping[
        str, Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar]
    ]
    | None = None,
) -> dict[str, float | None]:
    """Shares the market can price: issued less treasury, on the as-of split basis.

    universe の時価総額はこの index から作られ、時価総額 gate (100 億円) の判定値になる。
    `FinancialSnapshot.market_cap` と同じ株数で作らないと、同じ「時価総額」という語が
    2 つの値を指す。株数と自己株式数は BS 系 fact なので四半期開示に載らないことが多く、
    直近の非 null 行から carry-forward する。ただし正の自己株式より後に発行済だけを
    再観測した状態や、期中平均株式数と区別できない発行済は同じ資本状態と確認できないため、
    時価総額を答えない。
    """
    shares: dict[str, float | None] = {}
    for ticker, summaries in summaries_by_ticker.items():
        available = [row for row in summaries if row.disclosed_at <= asof_date]
        ticker_bars = bars_by_ticker.get(ticker, ())
        adjustment_events = (
            adjustment_events_by_ticker.get(ticker, ())
            if adjustment_events_by_ticker is not None
            else ticker_bars
        )
        normalized = _normalize_summaries_with_status(available, adjustment_events, asof_date)
        shares[ticker] = _resolve_capital_basis(
            normalized.summaries,
            capital_basis_barrier=normalized.capital_basis_barrier,
        ).shares_ex_treasury
    return shares


# 期末発行済から自己株を引いた株数が、提出者自身が EPS を出すのに使った期中平均株数から
# この倍率を超えて外れる行は、株数を per-share の分母に使わない。
SHARE_COUNT_ANCHOR_TOLERANCE = 2.0

# EDINET の総資産が短信の総資産からこの倍率を超えて外れる行は、同じ会社の貸借対照表では
# ないとみなし、EDINET 由来の値を判断面へ出さない。
ENTITY_SCALE_TOLERANCE = 2.0

CONSOLIDATED_BASIS = "consolidated"

ENTITY_SCALE_MISMATCH = "entity_scale_mismatch"


def _edinet_describes_same_entity(
    edinet: EdinetMetricRecord | None,
    total_assets: float | None,
) -> bool:
    """EDINET の記録が短信と同じ実体を指しているか。

    抽出器は 1 つの書類を連結・単体のどちらかの基準で読む。連結財務諸表を持つ会社の
    書類を単体基準で読むと、負債・現金・EBITDA が親会社単独の値になる。時価総額と TTM
    系列は短信由来なので、比率の分子と分母が別の会社を指す (実測では総資産が 1/1000 に
    なる行がある)。両側が総資産を持つので、実体の一致は直接確かめられる。

    連結基準で読めた行は照合できた 3,117 行すべてで一致するため、総資産を持たない
    連結行はそのまま通す。単体基準・基準不明の行はその母集団の 17.6% が桁でずれており、
    どれがずれているかを他の field では言えないので、照合できなければ答えない。
    """
    if edinet is None:
        return True
    reported = edinet.total_assets
    if reported is not None and reported > 0 and total_assets is not None and total_assets > 0:
        ratio = reported / total_assets
        return 1 / ENTITY_SCALE_TOLERANCE <= ratio <= ENTITY_SCALE_TOLERANCE
    return edinet.consolidation_basis == CONSOLIDATED_BASIS


# 円経路の自己資本を普通株基準として採るために要求する一致幅。実測では通期行の 97.2% が
# 1% 以内に収まり、外れる 677 行は優先株・非支配株主持分を含む資本構成である。
_COMMON_EQUITY_BASIS_TOLERANCE = 0.05

_EQUITY_RATIO_REPORTING_HALF_UNIT = 0.0005

_BPS_REPORTING_HALF_UNIT = 0.005


def _reported_equity_routes_overlap(
    *,
    reported_ratio: float,
    bps: float,
    shares: float,
    total_assets: float,
) -> bool:
    """Whether the EqAR and BPS routes overlap inside their published precision."""

    ratio_low = reported_ratio - _EQUITY_RATIO_REPORTING_HALF_UNIT
    ratio_high = reported_ratio + _EQUITY_RATIO_REPORTING_HALF_UNIT
    bps_ratio_low = (bps - _BPS_REPORTING_HALF_UNIT) * shares / total_assets
    bps_ratio_high = (bps + _BPS_REPORTING_HALF_UNIT) * shares / total_assets
    return max(ratio_low, bps_ratio_low) <= min(ratio_high, bps_ratio_high)


def _common_equity_yen(
    summaries: Sequence[JQuantsFinancialSummary],
    *,
    total_assets: float | None,
    equity_to_asset_ratio: float | None,
    bps: float | None,
    shares_ex_treasury: float | None,
) -> float | None:
    """普通株主に帰属する自己資本 (円)。

    `総資産 x 自己資本比率` は四半期行にも載るので新しいが、分子が普通株主の持分とは
    限らない。`bps x 自己株控除後株数` は普通株基準だが古い。両方を持つ直近の行で 2 つが
    一致するなら、その会社では円経路も普通株基準なので鮮度を採る。食い違う会社は基準の
    違いなので `bps` 側を採る。どちらか一方しか無ければそれを使う。

    判定は突き合わせ行の中で行い、carry 後の 2 値が離れていることは理由にしない。乖離は
    ほとんどが実際の資本変動だからである — as-of 2026-07-31 で 1.5 倍を超えた 55 社のうち、
    未適用の分割で説明できるのは 1 社だけで、残り 54 社は `bps` の出所行から as-of まで
    調整係数を 1 つも持たない。倍率で切ると、減損や大幅増資で自己資本が実際に動いた会社の
    新しい値を捨てて古い `bps` を採ることになり、7069 (自己資本比率 0.441 -> 0.062) の
    PBR は 36.3 から 3.08 へ、割安側へ 12 倍ずれる。
    """
    yen_equity = (
        total_assets * equity_to_asset_ratio
        if total_assets is not None and equity_to_asset_ratio is not None
        else None
    )
    bps_equity = bps * shares_ex_treasury if bps is not None and shares_ex_treasury else None
    if yen_equity is None:
        return bps_equity
    if bps_equity is None:
        return yen_equity
    for summary in sorted(_actual_rows(summaries), key=_accounting_observation_key, reverse=True):
        if (
            summary.bps is None
            or summary.total_assets is None
            or summary.equity_to_asset_ratio is None
            or summary.shares_outstanding is None
        ):
            continue
        row_shares = _shares_excluding_treasury(summary.shares_outstanding, summary.treasury_shares)
        row_yen = summary.total_assets * summary.equity_to_asset_ratio
        if row_shares is None or row_yen <= 0:
            continue
        row_bps_equity = summary.bps * row_shares
        if row_bps_equity <= 0:
            continue
        agrees = abs(
            row_yen / row_bps_equity - 1.0
        ) <= _COMMON_EQUITY_BASIS_TOLERANCE or _reported_equity_routes_overlap(
            reported_ratio=summary.equity_to_asset_ratio,
            bps=summary.bps,
            shares=row_shares,
            total_assets=summary.total_assets,
        )
        return yen_equity if agrees else bps_equity
    # 突き合わせられる行が無い会社では、狭い方の基準を採る。
    return bps_equity


def _shares_excluding_treasury(
    shares_outstanding: float | None, treasury_shares: float | None
) -> float | None:
    """市場が値付けできる株式数。自己株式数が観測できない行は答えない。

    自己株式数の欠損を 0 で埋めると「自己株ゼロ」を捏造し、どれだけ過大か分からない
    時価総額が現金比率・利回り・時価総額 gate へ入る。発行済を超える自己株式数も開示の破損
    なので答えない。どちらも時価総額が null になり、その銘柄は母集団に入らない。
    """

    if shares_outstanding is None or shares_outstanding <= 0:
        return None
    if treasury_shares is None or treasury_shares < 0:
        return None
    remaining = shares_outstanding - treasury_shares
    return remaining if remaining > 0 else None
