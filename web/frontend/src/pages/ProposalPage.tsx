import { useEffect, useState, type ReactNode } from 'react'
import { Copy } from 'lucide-react'

import { fetchJson } from '../api/client'
import type {
  LatestProposalView,
  SavedAssessmentView,
  SavedBuyItemView,
  SavedExposureRowView,
  SavedHoldItemView,
  SavedLimitTermsView,
  SavedPortfolioExposureView,
  SavedProposalView,
  SavedReasonView,
  SavedSellItemView,
} from '../api/types'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { PageState } from '../components/PageState'
import { SectionCard } from '../components/SectionCard'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { formatJstDateTime } from '../lib/format'

type ProposalItem = SavedProposalView['items'][number]

const SCOPE_LABEL: Record<SavedProposalView['request_scope'], string> = {
  discover_new: '新規探索',
  specified_stocks: '指定銘柄',
  review_positions: '保有見直し',
}
const ACTION_LABEL: Record<ProposalItem['action'], string> = {
  buy: '買付（指値）',
  hold: '保有継続',
  sell: '全量売却',
  no_action: '経済的見送り',
  defer: '判断保留',
}
const TIMING_LABEL: Record<SavedBuyItemView['timing'], string> = {
  at_current_limit: '保存終値の呼値切下げに一致する指値',
  wait_limit: '保存終値より低い指値で待つ',
  place_limit: '作者指定の買い指値を提示する区分',
}
const STANCE_LABEL: Record<SavedAssessmentView['stance'], string> = {
  supported: '支持', uncertain: '不確実', unsupported: '不支持',
}
const CASE_LABEL: Record<SavedAssessmentView['investment_case_status'], string> = {
  intact: '維持', broken: '不成立', uncertain: '不確実',
}
const SOURCE_CHECK_LABEL: Record<SavedAssessmentView['review']['primary_source_check'], string> = {
  verified: '確認済み', partially_verified: '部分確認', unverified: '未確認',
}
const REWARD_LABEL = { sufficient: '十分', insufficient: '不足', uncertain: '不確実' } as const

function Prose({ children }: { children: ReactNode }) {
  return <p className="whitespace-pre-wrap break-words text-sm leading-7">{children}</p>
}

function TextList({ title, items }: { title: string; items: string[] }) {
  if (items.length === 0) return null
  return (
    <div className="grid gap-2">
      <p className="text-sm font-medium">{title}</p>
      <ul className="grid list-disc gap-2 pl-5 text-sm leading-7">
        {items.map((text, index) => <li className="whitespace-pre-wrap break-words" key={index}>{text}</li>)}
      </ul>
    </div>
  )
}

function Reasons({ title, reasons }: { title: string; reasons: SavedReasonView[] }) {
  if (reasons.length === 0) return null
  return (
    <div className="grid gap-2 rounded-lg bg-muted p-4">
      <p className="text-sm font-medium">{title}</p>
      <ul className="grid gap-3">
        {reasons.map((reason, index) => (
          <li className="grid gap-1" key={index}>
            <Prose>{reason.message}</Prose>
            <code className="break-all text-xs text-muted-foreground">{reason.code}</code>
          </li>
        ))}
      </ul>
    </div>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <div className="min-w-0"><dt className="text-xs text-muted-foreground">{label}</dt><dd className="mt-1 break-words text-sm font-medium tabular-nums">{children}</dd></div>
}

function SavedTime({ value }: { value: string }) {
  return <time dateTime={value} title={value}>{formatJstDateTime(value)} JST</time>
}

function Identifier({ label, value }: { label: string; value: string }) {
  const [status, setStatus] = useState('')
  async function copy() {
    try {
      await navigator.clipboard.writeText(value)
      setStatus('コピーしました')
    } catch {
      setStatus('コピーできませんでした。識別子を選択してコピーしてください。')
    }
  }
  return (
    <div className="min-w-0">
      <p className="text-xs text-muted-foreground">{label}</p>
      <div className="mt-1 flex min-w-0 flex-wrap items-center gap-2">
        <code className="min-w-0 select-text break-all text-xs">{value}</code>
        <Button aria-label={`${label}をコピー`} onClick={copy} size="xs" variant="outline"><Copy aria-hidden="true" />コピー</Button>
      </div>
      <p aria-live="polite" className="mt-1 text-xs text-muted-foreground" role="status">{status}</p>
    </div>
  )
}

function AssessmentExcerpt({ assessment }: { assessment: SavedAssessmentView }) {
  const review = assessment.review
  return (
    <div className="grid min-w-0 gap-4">
      <dl className="grid gap-4 sm:grid-cols-3">
        <Field label="評価基準日"><time dateTime={assessment.as_of}>{assessment.as_of}</time></Field>
        <Field label="評価の立場">{STANCE_LABEL[assessment.stance]}（{assessment.stance}）</Field>
        <Field label="投資理由の状態">{CASE_LABEL[assessment.investment_case_status]}（{assessment.investment_case_status}）</Field>
      </dl>
      <div><p className="mb-1 text-sm font-medium">投資理由</p><Prose>{assessment.investment_case_explanation}</Prose></div>
      <div><p className="mb-1 text-sm font-medium">状態の理由</p><Prose>{assessment.investment_case_status_reason}</Prose></div>
      <TextList items={assessment.invalidation_conditions} title="投資理由の反証条件" />
      <div className="grid gap-4 border-t pt-4">
        <p className="text-sm font-medium">独立検証の範囲</p>
        <p className="text-xs leading-6 text-muted-foreground">Reviewの存在は、すべての事実の確認や現在の注文資格を示しません。</p>
        <dl className="grid gap-4 sm:grid-cols-2">
          <Field label="一次資料の確認">{SOURCE_CHECK_LABEL[review.primary_source_check]}（{review.primary_source_check}）</Field>
          <Field label="独立検証時刻"><SavedTime value={review.reviewed_at} /></Field>
        </dl>
        <Prose>{review.review_scope}</Prose>
        <div><p className="mb-1 text-sm font-medium">独立検証の最も強い反対論</p><Prose>{review.strongest_countercase}</Prose></div>
        {review.uncertainty_acceptance_reason !== null && <div><p className="mb-1 text-sm font-medium">不確実性を引き受けた理由</p><Prose>{review.uncertainty_acceptance_reason}</Prose></div>}
      </div>
      <div className="grid min-w-0 gap-2 border-t pt-4">
        <Identifier label="assessment_id" value={assessment.assessment_id} />
        <Identifier label="core_sha256" value={assessment.core_sha256} />
        <Identifier label="review_id" value={assessment.review_id} />
      </div>
    </div>
  )
}

function SavedTerms({ terms }: { terms: SavedLimitTermsView }) {
  return (
    <div className="grid gap-4 rounded-lg border p-4">
      <p className="text-sm font-medium">保存された具体条件</p>
      <dl className="grid gap-4 sm:grid-cols-3">
        {/* Monetary strings are displayed verbatim: converting to Number would lose precision. */}
        <Field label="保存指値">{terms.limit_price_yen} 円</Field>
        <Field label="保存数量">{terms.quantity} 株</Field>
        <Field label="保存想定金額">{terms.notional_yen} 円</Field>
        <Field label="保存時の対象取引日"><time dateTime={terms.target_session}>{terms.target_session}</time></Field>
        <Field label="条件の評価基準日"><time dateTime={terms.valuation_as_of}>{terms.valuation_as_of}</time></Field>
        {terms.maximum_entry_price_yen !== null && <Field label="保存評価の条件付き上限（Pmax）">{terms.maximum_entry_price_yen} 円</Field>}
      </dl>
      {terms.maximum_entry_price_yen !== null && <p className="text-xs leading-6 text-muted-foreground">Pmaxは保存評価の条件付き上限です。適正価格・勝率・約定価格ではありません。</p>}
      <TextList items={terms.conditions} title="保存条件" />
      <TextList items={terms.warnings} title="具体条件の保存時の注意事項" />
    </div>
  )
}

function HoldingReason({ item }: { item: SavedHoldItemView | SavedSellItemView }) {
  return (
    <div className="grid gap-4">
      <p className="text-sm font-medium">{item.action === 'hold'
        ? item.holding_basis === 'investment_case_intact' ? '保有継続の根拠：投資仮説の維持' : '保有継続の根拠：十分な残存見返り'
        : item.reason_code === 'economic_invalidation' ? '売却の根拠：重要な投資仮説の不成立' : '売却の根拠：残存見返りの不足'}</p>
      {item.action === 'hold' && item.holding_basis === 'investment_case_intact' && <p className="text-xs leading-6 text-muted-foreground">投資仮説の維持と、現在の収益率が十分なことは別です。</p>}
      <div><p className="mb-1 text-sm font-medium">今回の再評価理由</p><Prose>{item.case_reassessment_reason}</Prose></div>
      {item.remaining_reward !== null && <div><p className="mb-1 text-sm font-medium">残存見返り：{REWARD_LABEL[item.remaining_reward.status]}</p><Prose>{item.remaining_reward.reason}</Prose></div>}
      {item.action === 'hold' && item.return_calculation_unavailable_reason !== null && <div><p className="mb-1 text-sm font-medium">収益率を計算できない理由</p><Prose>{item.return_calculation_unavailable_reason}</Prose></div>}
      <details className="rounded-lg border p-4">
        <summary className="cursor-pointer text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-4">当初の投資理由</summary>
        <div className="mt-4 grid gap-4">
          {item.original_assessment !== null && <AssessmentExcerpt assessment={item.original_assessment} />}
          {item.original_investment_reason !== null && <Prose>{item.original_investment_reason}</Prose>}
          {item.original_reason_unavailable_reason !== null && <div><p className="mb-1 text-sm font-medium">当初理由を確認できない理由</p><Prose>{item.original_reason_unavailable_reason}</Prose></div>}
        </div>
      </details>
    </div>
  )
}

export function ProposalItemCard({ item }: { item: ProposalItem }) {
  return (
    <SectionCard headingLevel={3} meta={<Badge variant="secondary">{ACTION_LABEL[item.action]} · {item.action}</Badge>} padded title={item.ticker}>
      <div className="grid min-w-0 gap-5">
        <Prose>{item.rationale}</Prose>
        {item.saved_quote !== null && <dl className="grid gap-4 sm:grid-cols-2">
          <Field label="保存株価（未調整終値）">{item.saved_quote.price_yen} 円</Field>
          <Field label="保存株価の基準日"><time dateTime={item.saved_quote.price_as_of}>{item.saved_quote.price_as_of}</time></Field>
        </dl>}
        {item.action === 'buy' && <p className="text-sm text-muted-foreground">保存時の指値区分：{TIMING_LABEL[item.timing]}（{item.timing}）</p>}
        {(item.action === 'buy' || item.action === 'sell') && (item.terms !== null ? <SavedTerms terms={item.terms} /> : <div className="grid gap-3">
          <p className="text-sm font-medium">具体条件未確定</p>
          {item.action === 'buy' && item.proposed_limit_yen !== null && <dl><Field label="作者指定の上限（具体条件未確定）">{item.proposed_limit_yen} 円</Field></dl>}
          <Reasons reasons={item.terms_unavailable_reasons} title="具体条件が不足する理由" />
        </div>)}
        {(item.action === 'hold' || item.action === 'sell') && <HoldingReason item={item} />}
        {item.action === 'defer' && <>
          <Reasons reasons={item.blockers} title="判断を保留した不足理由" />
          <TextList items={item.resume_conditions} title="再開条件" />
        </>}
        <TextList items={item.review_conditions} title="再確認条件" />
        {item.unfinished !== null && <div className="grid gap-3 rounded-lg bg-muted p-4">
          <p className="text-sm font-medium">保存scopeの未完了調査</p>
          <Prose>{item.unfinished.reason}</Prose>
          <TextList items={item.unfinished.references} title="未完了の参照" />
        </div>}
        {item.liquidity !== null && <details className="rounded-lg border p-4">
          <summary className="cursor-pointer text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-4">保存された流動性の補足</summary>
          <div className="mt-4 grid gap-3">
            <dl className="grid gap-4 sm:grid-cols-2">
              <Field label="保存ADV（平均売買代金）">{item.liquidity.adv_yen === null ? '保存値なし' : `${item.liquidity.adv_yen} 円`}</Field>
              <Field label="流動性の分析基準日">{item.liquidity.analysis_as_of === null ? '保存値なし' : <time dateTime={item.liquidity.analysis_as_of}>{item.liquidity.analysis_as_of}</time>}</Field>
            </dl>
            {item.liquidity.unavailable_reason !== null && <Prose>{item.liquidity.unavailable_reason}</Prose>}
            <p className="text-xs leading-6 text-muted-foreground">保存値であり、現在の板や約定可能性を示しません。</p>
          </div>
        </details>}
        {item.assessment === null ? <p className="text-sm text-muted-foreground">未評価：この項目には保存評価への参照がありません。</p> : <details className="rounded-lg border p-4">
          <summary className="cursor-pointer text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-4">今回の保存評価・独立検証の抜粋</summary>
          <div className="mt-4"><AssessmentExcerpt assessment={item.assessment} /></div>
        </details>}
      </div>
    </SectionCard>
  )
}

function ExposureRow({ label, row }: { label: string; row: SavedExposureRowView }) {
  return <div className="grid gap-2 rounded-lg bg-muted p-4">
    <p className="break-words text-sm font-medium">{label}：{row.key}</p>
    <dl className="grid gap-4 sm:grid-cols-2">
      <Field label="保存時の提案反映後比率">{row.prospective_pct} %</Field>
      <Field label="保存時の注意基準">{row.warning_pct} %</Field>
    </dl>
  </div>
}

function PortfolioExposure({ exposure }: { exposure: SavedPortfolioExposureView }) {
  return <details className="rounded-lg border bg-card p-5 sm:p-6">
    <summary className="cursor-pointer text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-4">保存された資本集中の補足</summary>
    <div className="mt-4 grid gap-4">
      <p className="text-xs leading-6 text-muted-foreground">保存された提案条件による比率です。現在の資産配分ではなく、売却代金の先取りや再計算はしていません。</p>
      <dl><Field label="保存時の総資本">{exposure.total_capital_yen === null ? '保存値なし' : `${exposure.total_capital_yen} 円`}</Field></dl>
      {exposure.ticker === null ? <p className="text-sm text-muted-foreground">銘柄別集中：保存値なし</p> : <ExposureRow label="銘柄別集中" row={exposure.ticker} />}
      {exposure.sector === null ? <p className="text-sm text-muted-foreground">業種別集中：保存値なし</p> : <ExposureRow label="業種別集中" row={exposure.sector} />}
      {exposure.common_factors.length === 0 ? <p className="text-sm text-muted-foreground">共通要因別集中：保存値なし</p> : exposure.common_factors.map((row) => <ExposureRow key={row.key} label="共通要因別集中" row={row} />)}
      {exposure.common_factor_unclassified_tickers.length > 0 && <div>
        <TextList items={exposure.common_factor_unclassified_tickers} title="共通要因が未分類の銘柄" />
        <p className="mt-2 text-xs leading-6 text-muted-foreground">未分類銘柄があるため、共通要因の比率は既知部分のみです。</p>
      </div>}
    </div>
  </details>
}

export function ProposalPage() {
  const [data, setData] = useState<LatestProposalView | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    fetchJson<LatestProposalView>('/api/proposal/latest', { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setData(value) })
      .catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '売買提案を読み込めませんでした。') })
    return () => controller.abort()
  }, [])
  if (error !== null) return <PageState message={error} title="売買提案の通信エラー" />
  return <ProposalReport data={data} />
}

export function ProposalReport({ data }: { data: LatestProposalView | null }) {
  if (data === null) return <LoadingPage label="売買提案を読み込んでいます" />
  if (data.status === 'unavailable') return <PageState message={data.error ?? '保存済み提案を読み取れませんでした。'} title="売買提案の読取不能" />
  if (data.status === 'empty') return <PageShell lead="最新基準日の保存済み提案を読みます。" meta={<span className="text-xs text-muted-foreground">表示データ生成 <SavedTime value={data.generated_at} /></span>} title="売買提案" width="reading"><SectionCard padded title="保存済み提案はありません"><Prose>表示データ生成時点で、正式に保存された提案はありません。</Prose></SectionCard></PageShell>
  const proposal = data.proposal
  if (proposal === null) return <PageState message="保存済み提案を読み取れませんでした。" title="売買提案の読取不能" />
  return (
    <PageShell lead="最新基準日の保存済み提案を、保存順のまま表示します。" meta={<Badge variant="outline">{SCOPE_LABEL[proposal.request_scope]}</Badge>} title="売買提案" width="reading">
      <aside className="rounded-lg border bg-muted px-5 py-4 text-sm leading-7">
        保存時点の提案です。現在の注文条件の再確認・採否・発注は別です。対象取引日は実注文の期限ではありません。
      </aside>
      <SectionCard padded title="保存された結論">
        <div className="grid gap-5">
          <Prose>{proposal.headline}</Prose>
          <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="提案基準日"><time dateTime={proposal.as_of}>{proposal.as_of}</time></Field>
            <Field label="提案本文の準備時刻"><SavedTime value={proposal.prepared_at} /></Field>
            <Field label="提案の発行時刻"><SavedTime value={proposal.published_at} /></Field>
            <Field label="表示データ生成時刻"><SavedTime value={data.generated_at} /></Field>
          </dl>
          <Identifier key={proposal.proposal_id} label="proposal_id" value={proposal.proposal_id} />
          <p className="text-xs leading-6 text-muted-foreground">表示データ生成時点の保存集合で、提案基準日・発行時刻・提案IDの順に最新の1件です。現在の注文可否は判定していません。</p>
        </div>
      </SectionCard>
      <section aria-labelledby="proposal-items-heading" className="grid min-w-0 gap-4">
        {(proposal.corrections ?? []).length > 0 && <SectionCard padded title="提案履歴の訂正">
          {(proposal.corrections ?? []).map((correction) => <div key={correction.correction_id} className="grid gap-2">
            <Prose>{correction.reason}</Prose>
            <p>誤って保存した提案を最新候補から除外しました。表示中の提案の根拠・条件・発行時刻は当時のままです。現在の再推奨や人間の採用ではありません。</p>
            <Identifier label="訂正対象" value={correction.erroneous_ref.proposal_id} />
            <SavedTime value={correction.recorded_at} />
          </div>)}
        </SectionCard>}
        {(proposal.human_reports ?? []).length > 0 && <SectionCard padded title="人間の報告（提案・取引事実とは別）">
          {(proposal.human_reports ?? []).map((report) => <div key={report.report_id} className="grid gap-2">
            <p className="font-medium">購入しなかった</p>
            <Prose>{report.statement}</Prose>
            <p>報告日時：<SavedTime value={report.reported_at} /></p>
            <p>本人が述べた対象時点：{report.subject_at ? <SavedTime value={report.subject_at} /> : '報告なし'}</p>
            {report.reason_quote && <div><p>本人の理由（引用）</p><Prose>{report.reason_quote}</Prose></div>}
            {report.context_summary && <div><p>会話文脈の要約（担当者）</p><Prose>{report.context_summary}</Prose></div>}
            <p>未発注・取消・未約定・永久却下は、この報告からは確認できません。注文・約定・現金の記録は変更していません。</p>
          </div>)}
        </SectionCard>}
        <h2 className="text-lg font-semibold" id="proposal-items-heading">銘柄別の行動と条件</h2>
        {proposal.items.map((item) => <ProposalItemCard item={item} key={`${proposal.proposal_id}-${item.ticker}`} />)}
      </section>
      <SectionCard padded title="全体の比較"><Prose>{proposal.comparison}</Prose></SectionCard>
      <SectionCard padded title="採らなかった案・機会損失"><Prose>{proposal.forgone}</Prose></SectionCard>
      {proposal.warnings.length > 0 && <SectionCard padded title="提案の保存時の注意事項"><TextList items={proposal.warnings} title="保存されたwarnings" /></SectionCard>}
      <PortfolioExposure exposure={proposal.portfolio_exposure} />
    </PageShell>
  )
}
