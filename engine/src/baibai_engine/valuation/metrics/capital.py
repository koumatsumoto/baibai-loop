"""株数・資本のbasisを揃え、会計entityの比較可否を判定する。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from enum import Enum
from math import exp, log

from baibai_engine.market.bars import (
    JQuantsAdjustmentFactorEvent,
    JQuantsDailyBar,
    share_unit_factor,
)
from baibai_engine.market.edinet_models import EdinetMetricRecord
from baibai_engine.market.jquants_models import JQuantsFinancialSummary
from baibai_engine.valuation.metrics.periods import (
    _accounting_observation_key,
    _accounting_period_identity,
    _AccountingObservationKey,
    _actual_rows,
    _latest_non_null_row,
    resolve_actual_conflicts,
)


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


def _cumulative_share_unit_factor_after(
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    after: date,
    asof_date: date,
    *,
    include_boundary_day: bool = False,
) -> float | None:
    """Accumulate only known share-unit changes in the requested interval.

    Rights-issue price factors do not scale shares or cash DPS. Unknown action
    kinds leave the unit basis unresolved. Disclosure-day inclusion follows the
    existing boundary convention; it does not prove a filing's share basis.
    """
    factor = 1.0
    for bar in ticker_bars:
        before_start = bar.traded_at < after if include_boundary_day else bar.traded_at <= after
        if before_start or bar.traded_at > asof_date:
            continue
        adjustment = share_unit_factor(bar)
        if adjustment is None:
            return None
        factor *= adjustment
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
        adjustment = share_unit_factor(bar)
        if adjustment is None:
            return 1.0, _ShareBasis.INDETERMINATE
        if adjustment == 1.0:
            continue
        interim *= adjustment
        if first_ex is None or bar.traded_at < first_ex:
            first_ex = bar.traded_at
    if interim in (0.0, 1.0) or first_ex is None:
        return 1.0, _ShareBasis.AS_OF_DISCLOSURE

    def classify(field_name: str) -> _ShareBasis:
        current = getattr(summary, field_name)
        previous_row = _latest_non_null_row(
            [
                row
                for row in summaries
                if row.disclosed_at < first_ex
                and (
                    field_name in row.unresolved_actual_fields
                    or (getattr(row, field_name) is not None and getattr(row, field_name) > 0)
                )
            ],
            field_name,
        )
        if current is None or current <= 0 or previous_row is None:
            return _ShareBasis.INDETERMINATE
        previous = getattr(previous_row, field_name)
        assert previous is not None
        # 比較元の開示後から現在行の期末までに別の corporate action がある場合、生の株数は
        # 現在行の期末基準ではない。先に同じ基準へ移さないと、連続した分割・併合をすべて
        # 今回の interim action の差と誤認する。開示日当日の権利落ちは開示行の正規化と同じ
        # 契約で数える。
        previous_to_period_end = _cumulative_share_unit_factor_after(
            ticker_bars,
            previous_row.disclosed_at,
            period_end,
            include_boundary_day=True,
        )
        if previous_to_period_end is None or previous_to_period_end <= 0:
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
    """Return rows normalized to the as-of share basis.

    Classify period-end versus disclosure basis when actions intervene, then apply
    subsequent factors to per-share values and their inverses to share counts.
    Indeterminate bases lose dependent values; forecasts across a split are not
    relabelled as split-safe. Callers needing the capital carry barrier use the
    result-returning variant directly.
    """
    return _normalize_summaries_with_status(summaries, ticker_bars, asof_date).summaries


def _normalize_summaries_with_status(
    summaries: Sequence[JQuantsFinancialSummary],
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    asof_date: date,
) -> _NormalizedSummaryResult:
    """Normalize rows while preserving an explicit fail-close capital carry barrier."""

    summaries = resolve_actual_conflicts(summaries)
    has_adjustment = any(
        share_unit_factor(bar) != 1.0 for bar in ticker_bars if bar.traded_at <= asof_date
    )
    if not has_adjustment:
        return _NormalizedSummaryResult(summaries=summaries, capital_basis_barrier=None)
    normalized: list[JQuantsFinancialSummary] = []
    capital_basis_barrier: _AccountingObservationKey | None = None
    for index, summary in enumerate(summaries):
        factor = _cumulative_share_unit_factor_after(
            ticker_bars, summary.disclosed_at, asof_date, include_boundary_day=True
        )
        interim, basis = _interim_split_basis(summaries, index, ticker_bars)
        if factor is None or basis is _ShareBasis.INDETERMINATE:
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

    `AvgSh` は EPS の期中平均株式数であり、発行済株式総数の代替には使わない。
    数値の一致や過去の株数との恒等式だけでは誤転記を証明できないため、報告された
    `ShOutFY`をそのまま発行済株式総数として扱う。
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
        same_period_day = (
            _accounting_period_identity(issued_row) == _accounting_period_identity(treasury_row)
            and issued_row.disclosed_at == treasury_row.disclosed_at
        )
        incomparable_time = same_period_day and (
            not issued_row.disclosed_time
            or not treasury_row.disclosed_time
            or issued_row.disclosed_time == treasury_row.disclosed_time
        )
        if incomparable_time:
            # Keep a fully observed pair when all candidates agree; never manufacture
            # the pair from two partial documents with unresolved chronology.
            complete = [
                row
                for row in capital_rows
                if _accounting_period_identity(row) == _accounting_period_identity(issued_row)
                and row.disclosed_at == issued_row.disclosed_at
                and row.shares_outstanding == issued
                and row.treasury_shares == treasury
            ]
            if complete:
                issued_row = treasury_row = complete[0]
        issued_source_key = _accounting_observation_key(issued_row)
        treasury_source_key = _accounting_observation_key(treasury_row)
        if (
            incomparable_time
            and issued_row.record_key is not None
            and treasury_row.record_key is not None
            and issued_row.record_key != treasury_row.record_key
        ):
            return _CapitalBasisResolution(
                issued=issued,
                treasury=treasury,
                shares_ex_treasury=None,
                failure_reason="conflicting_capital_document_sources",
            )
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
    再観測した状態では同じ資本状態と確認できないため、時価総額を答えない。
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

# EDINETと短信の総資産比がこの倍率帯を外れる行は、規模差の検査で不採用にする。
# 帯内という結果だけでは同じ時点・連結範囲であるとは確定しない。
ENTITY_SCALE_TOLERANCE = 2.0

CONSOLIDATED_BASIS = "consolidated"

ENTITY_SCALE_MISMATCH = "entity_scale_mismatch"


def _edinet_passes_entity_scale_check(
    edinet: EdinetMetricRecord | None,
    total_assets: float | None,
) -> bool:
    """Apply the total-assets scale heuristic, not a proof of accounting identity.

    Positive total assets on both sides must lie within the configured ratio band.
    If either amount is unavailable, only a consolidated EDINET row is accepted.
    Passing does not establish matching dates or consolidation scope; rejection does
    not identify whether scope, timing, or another source discrepancy caused it.
    """
    if edinet is None:
        return True
    reported = edinet.total_assets
    if reported is not None and reported > 0 and total_assets is not None and total_assets > 0:
        ratio = reported / total_assets
        return 1 / ENTITY_SCALE_TOLERANCE <= ratio <= ENTITY_SCALE_TOLERANCE
    return edinet.consolidation_basis == CONSOLIDATED_BASIS


# 円経路とBPS経路を比較する許容幅。通過や不一致だけでは資本構成の原因を特定しない。
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
    """Choose the yen or BPS equity route under the comparable-row policy.

    When both routes are available, compare the newest eligible row's TA/EqAR
    product with its BPS/share product. Agreement within tolerance or reporting
    precision permits the yen route; otherwise use BPS. The comparison does not
    establish the economic cause of a discrepancy or prove accounting identity.
    A later carried-value difference is not a second rejection test.

    If only one route is available, use it. With both routes but no comparable row,
    prefer BPS. The caller separately resolves capital basis and relative freshness.
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
