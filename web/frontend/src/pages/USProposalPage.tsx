import { useEffect, useState, type ReactNode } from 'react'
import { ArrowLeft, ArrowUpRight } from 'lucide-react'
import { Link, useSearchParams } from 'react-router'

import { ApiError, fetchJson } from '../api/client'
import type { LatestUSProposalView, USProposalComparisonScenarioView, USProposalComparisonView, USProposalView } from '../api/types'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { SectionCard } from '../components/SectionCard'
import { formatJstDateTime } from '../lib/format'
import { PROPOSAL_MARKETS } from '../lib/nav'

type ViewState<T> = { kind: 'loading' | 'pending' | 'unavailable' } | { kind: 'loaded'; data: T }
type USProposalRead = Omit<LatestUSProposalView, 'history'>
const US_PROPOSALS = PROPOSAL_MARKETS.us.to

function hasText(value: unknown): value is string {
  return typeof value === 'string' && value.trim() !== ''
}

function hasZonedClock(value: unknown): value is string {
  return typeof value === 'string' && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && !Number.isNaN(Date.parse(value))
}

function hasDecimalText(value: unknown): value is string {
  return typeof value === 'string' && /^-?\d+(?:\.\d+)?(?:E[+-]?\d+)?$/.test(value)
}

function hasScenarioAmounts(scenario: USProposalComparisonScenarioView | undefined) {
  return hasDecimalText(scenario?.terminal_value_usd_per_share)
    && hasDecimalText(scenario?.eligible_distributions_usd_per_share)
    && hasDecimalText(scenario?.difference_from_cash_usd_per_share)
}

function renderableProposal(proposal: USProposalView | null | undefined) {
  if (!proposal || proposal.currency !== 'USD' || (proposal.action !== 'no_action' && proposal.action !== 'defer')) return null
  // Only check the fields the page renders. Missing serving data is not a saved null,
  // and the browser does not recalculate the comparison or select a replacement price.
  const { official_close: close, iex_band: band, cutoff_at, reviewed_at, published_at, comparison, comparison_horizon_et: horizon } = proposal
  const invalidComparison = comparison === null
    ? horizon !== null || proposal.action === 'no_action'
    : !close || !hasText(horizon) || comparison?.cash_basis !== 'same_usd_no_interest_tax_or_fees'
      || !hasScenarioAmounts(comparison.base) || !hasScenarioAmounts(comparison.downside)
  if (![proposal.proposal_id, proposal.draft_sha256, proposal.ticker, proposal.venue,
    proposal.share_class, proposal.reason, proposal.revisit_when, proposal.session_et].every(hasText)
    || ![cutoff_at, reviewed_at, published_at].every(hasZonedClock)
    || (close !== null && (!hasDecimalText(close?.price_usd) || !hasZonedClock(close.observed_at)
      || (close.origin !== 'listing_exchange'
        && (close.origin !== 'alpaca_sip_nasdaq_official_close' || proposal.venue !== 'XNAS'))))
    || (band !== null && (!band || !Number.isInteger(band.observations) || band.observations <= 0
      || ![band.first_session_et, band.last_session_et].every(hasText)
      || ![band.low_usd, band.high_usd, band.last_usd].every(hasDecimalText)))
    || invalidComparison || !Array.isArray(proposal.unresolved) || !proposal.unresolved.every(hasText)) return null
  return proposal
}

function SavedTime({ value }: { value: string }) {
  return <time dateTime={value} title={value}>{formatJstDateTime(value)}</time>
}

function CashComparison({ comparison, horizon, cash }: {
  comparison: USProposalComparisonView; horizon: string; cash: string
}) {
  return <>
    <p className="break-words text-muted-foreground">
      比較の終期（米東部時間）：<time dateTime={horizon}>{horizon}</time>。
      この無調整終値1株分のUSD {cash}を無利息のUSD現金で持つ場合との比較です。
      税・費用・為替は含めません。仮定に基づく比較で、株価予測・口座残高・注文条件ではありません。
    </p>
    <div className="grid min-w-0 gap-3 sm:grid-cols-2">
      {[
        { label: '基本ケース', scenario: comparison.base },
        { label: '下方ケース', scenario: comparison.downside },
      ].map(({ label, scenario }) => {
        const difference = scenario.difference_from_cash_usd_per_share
        return <div className="min-w-0 break-words rounded-lg bg-muted p-3" key={label}>
          <h5 className="font-medium">{label}（1株当たり）</h5>
          <dl className="mt-2 grid gap-1 text-muted-foreground">
            <div><dt>終価</dt><dd>USD {scenario.terminal_value_usd_per_share}</dd></div>
            <div><dt>取得権のある分配</dt><dd>USD {scenario.eligible_distributions_usd_per_share}</dd></div>
            <div><dt>現金との差</dt><dd>USD {difference.startsWith('-') ? difference : `+${difference}`}</dd></div>
          </dl>
        </div>
      })}
    </div>
  </>
}

export function USProposal({ state, above, historyUnavailable }: {
  state: ViewState<USProposalRead>; above?: ReactNode; historyUnavailable?: boolean
}) {
  if (state.kind === 'loading') return <LoadingPage label="米国株の判断を読み込んでいます" />
  const view = state.kind === 'loaded' ? state.data : null
  const proposal = view?.status === 'ready' ? renderableProposal(view.proposal) : null
  const relayedClose = proposal?.official_close?.origin === 'alpaca_sip_nasdaq_official_close'
  const officialCloseLabel = relayedClose ? 'Nasdaqの無調整公式終値（Alpaca SIP経由）' : '取引所の無調整終値'
  const officialCloseClockLabel = relayedClose ? '終値データの受領時刻（JST）' : '終値の取得時刻（JST）'
  return (
    <PageShell title="米国株の判断" lead="保存済みの米ドル（USD）建て判断です。注文・口座操作はできません。" width="reading" above={above}>
      {historyUnavailable && proposal !== null && <p role="status" className="text-sm text-muted-foreground">履歴を読み取れません。以下は最新の保存判断です。</p>}
      {state.kind === 'pending' ? (
        <SectionCard padded title="米国株の判断は配信準備中です">
          <p>表示データが未配信です。保存された判断がないという意味ではありません。</p>
        </SectionCard>
      ) : view?.status === 'empty' && view.proposal === null ? (
        <SectionCard padded title="保存済みの米国株の判断はありません">
          <p>表示データ生成時点で、正式に保存された米国株の判断はありません。</p>
        </SectionCard>
      ) : proposal === null ? (
        <SectionCard padded title="米国株の判断を読み取れません">
          <p>別の時点や市場の判断では代用していません。</p>
        </SectionCard>
      ) : (
        <SectionCard padded title={`${proposal.ticker} · ${proposal.venue} · USD`} description={proposal.share_class}>
          <div className="grid min-w-0 gap-4 text-sm leading-6">
            <h3 className="text-lg font-semibold">{proposal.action === 'no_action' ? '見送り' : '判断保留'}</h3>
            <p className="whitespace-pre-wrap break-words">{proposal.reason}</p>
            <div>
              <h4 className="font-medium">見直し条件</h4>
              <p className="whitespace-pre-wrap break-words text-muted-foreground">{proposal.revisit_when}</p>
            </div>
            <section className="grid min-w-0 gap-2 border-t pt-4">
              <h4 className="font-medium">確認した価格と時点</h4>
              <dl className="grid min-w-0 gap-3 sm:grid-cols-2">
                <div><dt>情報締切（JST）</dt><dd className="text-muted-foreground"><SavedTime value={proposal.cutoff_at} /></dd></div>
                <div><dt>対象立会日（米東部時間）</dt><dd className="text-muted-foreground"><time dateTime={proposal.session_et}>{proposal.session_et}</time></dd></div>
                <div>
                  <dt>{officialCloseLabel}</dt>
                  <dd className="break-words text-muted-foreground">{proposal.official_close === null ? '未確認' : `USD ${proposal.official_close.price_usd} / 株`}</dd>
                  {proposal.official_close !== null && <dd className="text-muted-foreground">{officialCloseClockLabel}：<SavedTime value={proposal.official_close.observed_at} /></dd>}
                </div>
                <div>
                  <dt>Alpaca IEXの分割調整済み観測</dt>
                  <dd className="break-words text-muted-foreground">{proposal.iex_band === null ? '未確認' : `${proposal.iex_band.first_session_et}〜${proposal.iex_band.last_session_et}（米東部時間）に${proposal.iex_band.observations}件の観測、USD ${proposal.iex_band.low_usd}〜${proposal.iex_band.high_usd} / 株`}</dd>
                  {proposal.iex_band !== null && <dd className="break-words text-muted-foreground">期間末尾：USD {proposal.iex_band.last_usd} / 株（取引所終値とは別）</dd>}
                </div>
              </dl>
            </section>
            <section className="grid min-w-0 gap-2 border-t pt-4">
              <h4 className="font-medium">同期間のUSD現金との比較</h4>
              {proposal.comparison !== null && proposal.comparison_horizon_et !== null && proposal.official_close !== null
                ? <CashComparison comparison={proposal.comparison} horizon={proposal.comparison_horizon_et} cash={proposal.official_close.price_usd} />
                : <p className="text-muted-foreground">比較未確定。確認できていない価格や前提を補っていません。</p>}
            </section>
            <section className="grid min-w-0 gap-2 border-t pt-4">
              <h4 className="font-medium">査読と未確認事項</h4>
              <dl className="grid min-w-0 gap-3 sm:grid-cols-2">
                <div><dt>正式判断の査読日時（JST）</dt><dd className="text-muted-foreground"><SavedTime value={proposal.reviewed_at} /></dd></div>
                <div><dt>発行日時（JST）</dt><dd className="text-muted-foreground"><SavedTime value={proposal.published_at} /></dd></div>
              </dl>
              {proposal.unresolved.length > 0 && <div><h5 className="font-medium">未確認事項</h5><ul className="list-disc break-words pl-5 text-muted-foreground">{proposal.unresolved.map(note => <li key={note}>{note}</li>)}</ul></div>}
            </section>
            <dl className="grid min-w-0 gap-2 border-t pt-4 text-xs text-muted-foreground">
              <div><dt>判断ID</dt><dd className="break-all font-mono">{proposal.proposal_id}</dd></div>
              <div><dt>査読済み判断のSHA-256</dt><dd className="break-all font-mono">{proposal.draft_sha256}</dd></div>
            </dl>
          </div>
        </SectionCard>
      )}
    </PageShell>
  )
}

function BackToReports() {
  return <Link className="inline-flex w-fit items-center gap-2 rounded text-sm text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring" to={US_PROPOSALS}><ArrowLeft aria-hidden="true" className="size-4" />米国株のレポート一覧</Link>
}

function SelectionError({ children }: { children: ReactNode }) {
  return <PageShell title="米国株のレポートを表示できません" width="reading" above={<BackToReports />}>
    <SectionCard padded title="指定された保存レポートを確認できません"><p>{children}</p></SectionCard>
  </PageShell>
}

function UnavailableSelection() {
  return <SelectionError>この保存判断は読み取れません。別の判断では代用していません。</SelectionError>
}

function readableHistory(data: LatestUSProposalView) {
  const history = data?.history
  if (!history || !Array.isArray(history.proposals) || !Array.isArray(history.unavailable_proposal_ids)
    || !history.unavailable_proposal_ids.every(hasText) || typeof history.has_more !== 'boolean'
    || (data.status !== 'ready' && data.status !== 'empty' && data.status !== 'unavailable')) return null
  if (history.status === 'empty') {
    return data.status === 'empty' && data.proposal === null
      && history.proposals.length === 0 && history.unavailable_proposal_ids.length === 0 && !history.has_more
      ? { rows: [], unavailable: new Set<string>(), incomplete: false, latestId: null, hasMore: false }
      : null
  }
  if (data.status === 'empty' || (history.status !== 'ready' && history.status !== 'partial')) return null
  const unavailable = new Set(history.unavailable_proposal_ids)
  const rows = new Map<string, USProposalView>()
  let incomplete = history.status === 'partial' || unavailable.size > 0
  const current = data.status === 'ready' ? data.proposal : null
  // The current body is carried once, separately from the ordered remaining items.
  // An unreadable or ambiguous ID must not select either body or demote another proposal to latest.
  for (const row of data.status === 'ready' ? [current, ...history.proposals] : history.proposals) {
    const saved = renderableProposal(row)
    const id = row?.proposal_id
    if (!saved || unavailable.has(saved.proposal_id) || rows.has(saved.proposal_id)) {
      incomplete = true
      if (hasText(id)) {
        unavailable.add(id)
        rows.delete(id)
      }
      continue
    }
    rows.set(saved.proposal_id, saved)
  }
  if (!rows.size) return null
  const latestId = current && rows.has(current.proposal_id) ? current.proposal_id : null
  return { rows: [...rows.values()], unavailable, incomplete, latestId, hasMore: history.has_more }
}

export function USProposalHistory({ state, search }: {
  state: ViewState<LatestUSProposalView>; search: URLSearchParams
}) {
  const proposalId = search.get('proposal')
  const ticker = search.get('ticker')
  const above = proposalId !== null || ticker !== null ? <BackToReports /> : undefined
  if (search.getAll('proposal').length > 1 || search.getAll('ticker').length > 1
    || (proposalId !== null && !hasText(proposalId))
    || (ticker !== null && (proposalId === null || !hasText(ticker)))) {
    return <SelectionError>判断IDと銘柄を確認して、一覧から選び直してください。</SelectionError>
  }
  if (state.kind !== 'loaded') return <USProposal state={state} above={above} />
  const history = readableHistory(state.data)
  if (history === null) {
    if (proposalId !== null) {
      const ids = state.data?.history?.unavailable_proposal_ids
      if (Array.isArray(ids) && ids.includes(proposalId)) return <UnavailableSelection />
    } else if (state.data?.status === 'ready') {
      // Latest has a separate owner; it may still be valid when there is no verified list.
      return <USProposal state={state} historyUnavailable />
    }
    return <USProposal state={{ kind: 'unavailable' }} above={above} />
  }
  const { rows, unavailable, incomplete, latestId, hasMore } = history
  if (proposalId !== null) {
    if (unavailable.has(proposalId)) return <UnavailableSelection />
    const proposal = rows.find(row => row.proposal_id === proposalId && (ticker === null || row.ticker === ticker))
    if (!proposal) {
      return <SelectionError>指定した判断・銘柄の組合せはこの表示範囲にありません。最新の判断では代用していません。</SelectionError>
    }
    return <USProposal state={{ kind: 'loaded', data: { generated_at: state.data.generated_at, status: 'ready', error: null, proposal } }} above={above} />
  }
  return <PageShell
    title="米国株の売買提案"
    lead="米ドル（USD）建ての保存済み判断です。現在の売買推奨や注文条件ではありません。"
    meta={<span className="text-sm text-muted-foreground">{rows.length}件のレポート</span>}
    width="reading"
  >
    {rows.length === 0 ? <SectionCard padded title="保存済みの米国株の判断はありません">
      <p>表示データ生成時点で、正式に保存された米国株の判断はありません。</p>
    </SectionCard> : <>
      <div className="grid gap-2 text-xs leading-6 text-muted-foreground">
        <p>発行日時（JST）の新しい順です。対象立会日（米東部時間）や表示データ生成日時とは異なります。</p>
        {hasMore && <p>一覧には直近の発行分だけを表示しています。</p>}
        {latestId === null && <p role="status">最新の保存判断を読み取れません。過去の判断では代用していません。</p>}
        {incomplete && <p role="status">一部の保存判断を読み取れません。読めたレポートだけを表示しています。</p>}
      </div>
      <div className="grid min-w-0 gap-3 md:grid-cols-2">
        {rows.map(row => {
          const query = new URLSearchParams({ proposal: row.proposal_id, ticker: row.ticker })
          return <Link key={row.proposal_id} to={{ pathname: US_PROPOSALS, search: query.toString() }} className="group grid min-h-11 min-w-0 content-start gap-2 rounded-xl border bg-card p-4 transition-colors hover:border-primary/40 hover:bg-muted/20 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring sm:p-5">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0"><h2 className="break-words text-lg font-semibold">{row.ticker} · {row.venue} · USD</h2><p className="break-words text-xs text-muted-foreground">{row.share_class}</p></div>
              <ArrowUpRight aria-hidden="true" className="size-4 shrink-0 text-muted-foreground group-hover:text-foreground" />
            </div>
            <p className="text-sm">{row.action === 'no_action' ? '見送り' : '判断保留'}{row.proposal_id === latestId && <span className="ml-2 text-xs text-muted-foreground">最新の保存判断</span>}</p>
            <dl className="grid gap-1 border-t pt-2 text-xs text-muted-foreground">
              <div><dt className="inline">発行日時（JST）：</dt><dd className="inline"><SavedTime value={row.published_at} /></dd></div>
              <div><dt className="inline">対象立会日（米東部時間）：</dt><dd className="inline"><time dateTime={row.session_et}>{row.session_et}</time></dd></div>
            </dl>
          </Link>
        })}
      </div>
    </>}
    {hasZonedClock(state.data.generated_at) && <p className="text-xs text-muted-foreground">表示データ生成（JST）：<SavedTime value={state.data.generated_at} /></p>}
  </PageShell>
}

export function USProposalPage() {
  const [state, setState] = useState<ViewState<LatestUSProposalView>>({ kind: 'loading' })
  const [search] = useSearchParams()
  useEffect(() => {
    const controller = new AbortController()
    fetchJson<LatestUSProposalView>('/api/us-proposal/latest', { signal: controller.signal })
      .then(data => { if (!controller.signal.aborted) setState({ kind: 'loaded', data }) })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          setState({ kind: error instanceof ApiError && error.status === 404 ? 'pending' : 'unavailable' })
        }
      })
    return () => controller.abort()
  }, [])
  useEffect(() => { window.scrollTo(0, 0) }, [search])
  return <USProposalHistory state={state} search={search} />
}
