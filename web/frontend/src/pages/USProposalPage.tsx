import { useEffect, useState } from 'react'

import { ApiError, fetchJson } from '../api/client'
import type { LatestUSProposalView } from '../api/types'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { SectionCard } from '../components/SectionCard'
import { formatJstDateTime } from '../lib/format'

type ViewState = { kind: 'loading' | 'pending' | 'unavailable' } | { kind: 'loaded'; data: LatestUSProposalView }

function hasText(value: unknown): value is string {
  return typeof value === 'string' && value.trim() !== ''
}

function renderableProposal(view: LatestUSProposalView | null) {
  const proposal = view?.status === 'ready' ? view.proposal : null
  if (!proposal || proposal.currency !== 'USD' || (proposal.action !== 'no_action' && proposal.action !== 'defer')) return null
  // Only check the rendered fields. A positive status alone must not turn a broken object
  // into a decision, fill in missing evidence, or send an invalid clock to Intl.DateTimeFormat.
  const { official_close: close, iex_band: band, published_at: publishedAt } = proposal
  if (![proposal.proposal_id, proposal.draft_sha256, proposal.ticker, proposal.venue,
    proposal.share_class, proposal.reason, proposal.revisit_when, proposal.session_et, publishedAt].every(hasText)
    || !/(?:Z|[+-]\d{2}:\d{2})$/.test(publishedAt) || Number.isNaN(Date.parse(publishedAt))
    || (close !== null && !hasText(close?.price_usd))
    || (proposal.action === 'no_action' && (close === null || proposal.comparison_horizon_et === null))
    || (band !== null && (!band || !Number.isInteger(band.observations) || band.observations <= 0
      || ![band.first_session_et, band.last_session_et, band.low_usd, band.high_usd].every(hasText)))
    || (proposal.comparison_horizon_et !== null && !hasText(proposal.comparison_horizon_et))
    || !Array.isArray(proposal.unresolved) || !proposal.unresolved.every(hasText)) return null
  return proposal
}

export function USProposal({ state }: { state: ViewState }) {
  if (state.kind === 'loading') return <LoadingPage label="米国株の判断を読み込んでいます" />
  const view = state.kind === 'loaded' ? state.data : null
  const proposal = renderableProposal(view)
  return (
    <PageShell title="米国株の判断" lead="保存済みの米ドル（USD）建て判断です。注文・口座操作はできません。" width="reading">
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
            <dl className="grid min-w-0 gap-3 border-t pt-4 sm:grid-cols-2">
              <div><dt>見直し条件</dt><dd className="whitespace-pre-wrap break-words text-muted-foreground">{proposal.revisit_when}</dd></div>
              <div><dt>対象立会日（米東部時間）</dt><dd className="text-muted-foreground">{proposal.session_et}</dd></div>
              <div><dt>取引所の無調整終値</dt><dd className="text-muted-foreground">{proposal.official_close === null ? '未確認' : `USD ${proposal.official_close.price_usd}`}</dd></div>
              <div><dt>Alpaca IEXの分割調整済み観測</dt><dd className="text-muted-foreground">{proposal.iex_band === null ? '未確認' : `${proposal.iex_band.first_session_et}〜${proposal.iex_band.last_session_et}に${proposal.iex_band.observations}件の観測、USD ${proposal.iex_band.low_usd}〜${proposal.iex_band.high_usd}`}</dd></div>
              <div><dt>比較の終期（米東部時間）</dt><dd className="text-muted-foreground">{proposal.comparison_horizon_et ?? '比較未確定'}</dd></div>
              <div><dt>発行日時（JST）</dt><dd className="text-muted-foreground"><time dateTime={proposal.published_at}>{formatJstDateTime(proposal.published_at)}</time></dd></div>
            </dl>
            {proposal.unresolved.length > 0 && <div><h4 className="font-medium">未確認事項</h4><ul className="list-disc break-words pl-5 text-muted-foreground">{proposal.unresolved.map(note => <li key={note}>{note}</li>)}</ul></div>}
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

export function USProposalPage() {
  const [state, setState] = useState<ViewState>({ kind: 'loading' })
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
  return <USProposal state={state} />
}
