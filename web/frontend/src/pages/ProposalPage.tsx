import { useState, type ReactNode } from 'react'
import { Copy } from 'lucide-react'

import type {
  LatestProposalView,
  SavedAssessmentView,
  SavedBuyItemView,
  SavedDeferItemView,
  SavedExposureRowView,
  SavedHoldItemView,
  SavedHoldingOriginView,
  SavedLimitTermsView,
  SavedPortfolioExposureView,
  SavedProposalView,
  SavedReasonView,
  SavedSellItemView,
} from '../api/types'
import { LoadingPage } from '../components/LoadingIndicator'
import { AssessmentRisks } from '../components/AssessmentRisks'
import { AssessmentSources } from '../components/AssessmentSources'
import { AssessmentHypotheses } from '../components/AssessmentHypotheses'
import { AssessmentValueGap } from '../components/AssessmentValueGap'
import { ModelPerspectives } from '../components/ModelPerspectives'
import { ReportSummary } from '../components/ReportSummary'
import { PriceValueFinding } from '../components/PriceValueFinding'
import { ProposalWarnings } from '../components/ProposalWarnings'
import { SavedBuySession } from '../components/SavedBuySession'
import { SavedMarketContext } from '../components/SavedMarketContext'
import { PageShell } from '../components/PageShell'
import { PageState } from '../components/PageState'
import { SectionCard } from '../components/SectionCard'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { formatJstDateTime, formatPct } from '../lib/format'
import { proposalActionLabel, isCorrectedProposal } from '../lib/proposal-history'

type ProposalItem = SavedProposalView['items'][number]

const SCOPE_LABEL: Record<SavedProposalView['request_scope'], string> = {
  discover_new: '新規探索',
  specified_stocks: '指定銘柄',
  review_positions: '保有見直し',
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

function AssessmentCountercases({ assessment }: { assessment: SavedAssessmentView }) {
  return <div className="grid gap-4">
    <div><p className="mb-1 text-sm font-medium">評価作者の最も強い反対論</p>
      {assessment.strongest_countercase == null
        ? <p className="text-sm text-muted-foreground">この表示データには評価作者の反対論が含まれていません。反証がないことを示しません。</p>
        : <Prose>{assessment.strongest_countercase}</Prose>}
    </div>
    <div><p className="mb-1 text-sm font-medium">独立検証の最も強い反対論</p><Prose>{assessment.review.strongest_countercase}</Prose></div>
  </div>
}

function AssessmentRealization({ assessment }: { assessment: SavedAssessmentView }) {
  const realization = assessment.realization
  return <section className="grid min-w-0 gap-4 rounded-lg border p-4" aria-label="価値実現の機序と時間">
    <h4 className="text-sm font-semibold">価値実現の機序と時間</h4>
    <p className="text-xs leading-6 text-muted-foreground">保存評価の作者仮説です。評価の比較期間とは別で、実現や現在の裏付けを保証しません。</p>
    {realization == null
      ? <p className="text-sm text-muted-foreground">この表示データには価値実現の条件が含まれていません。遅延リスクがないことを示しません。</p>
      : <>
        <div><p className="mb-1 text-sm font-medium">価値が届く経路</p><Prose>{realization.mechanism}</Prose></div>
        <Field label="実現時期の判断">{realization.timing === 'known' ? '作者が時期を把握と記録' : '未確定'}（{realization.timing}）</Field>
        <div><p className="mb-1 text-sm font-medium">時期の根拠・留保</p><Prose>{realization.timing_reason}</Prose></div>
        <div><p className="mb-1 text-sm font-medium">遅延した場合のリスク</p><Prose>{realization.delay_risk}</Prose></div>
      </>}
  </section>
}

function AssessmentRefresh({ assessment }: { assessment: SavedAssessmentView }) {
  const refresh = assessment.refresh
  const sections: Record<string, string> = {
    findings: '事業・価値仮説', valuation: '価値算定', investment_case: '投資理由',
    risks: '恒久損失リスク', sources: '資料', model_perspectives: 'モデル別観点',
  }
  return <details className="rounded-lg border p-4">
    <summary className="cursor-pointer text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-4">評価を更新した根拠</summary>
    <div className="mt-4 grid min-w-0 gap-4">
      <p className="text-xs leading-6 text-muted-foreground">この保存評価を作成した時の差分確認です。現在の確認・全資料の再取得・判断の有効性を保証しません。</p>
      {refresh == null
        ? <p className="text-sm text-muted-foreground">この表示データには差分確認の記録がありません。未実施・不要とは判定しません。</p>
        : <>
          <Field label="差分確認時刻"><SavedTime value={refresh.checked_at} /></Field>
          <Field label="今回変更した範囲">{refresh.changed_sections.map(section => sections[section] ?? section).join('、') || '変更区画の記載なし'}</Field>
          {([
            ['material_information_check', '重要情報の確認'],
            ['unchanged_assumptions_reason', '変更しなかった前提の理由'],
            ['horizon_reason', '残期間の理由'],
            ['distribution_reason', '今後の分配の理由'],
            ['corporate_action_reason', '株式・権利単位の確認'],
          ] as const).map(([key, label]) => <div key={key}><p className="mb-1 text-sm font-medium">{label}</p><Prose>{refresh[key]}</Prose></div>)}
          <div className="grid min-w-0 gap-2 border-t pt-4">
            <p className="text-xs text-muted-foreground">素材にした旧評価の固定参照です。当初の購入理由や最新評価とは限りません。</p>
            <Identifier label="更新元 assessment_id" value={refresh.prior_assessment_ref.assessment_id} />
            <Identifier label="更新元 core_sha256" value={refresh.prior_assessment_ref.core_sha256} />
            <Identifier label="更新元 review_id" value={refresh.prior_assessment_ref.review_id} />
          </div>
        </>}
    </div>
  </details>
}

function AssessmentExcerpt({ assessment, showCountercase = true }: { assessment: SavedAssessmentView; showCountercase?: boolean }) {
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
      <div>
        <p className="text-sm font-medium">投資理由の保存引用</p>
        {assessment.investment_case_source_ids == null || assessment.investment_case_sources == null
          ? <p className="text-xs text-muted-foreground">この表示データには投資理由の引用対応が含まれていません。根拠がないことを示しません。</p>
          : <>
            <p className="text-xs leading-6 text-muted-foreground">作者が投資理由の区画に引用した当時の資料です。各文の裏付けや仮説維持の確認、独立した支持票を意味しません。</p>
            <AssessmentSources sourceIds={assessment.investment_case_source_ids} sources={assessment.investment_case_sources} />
          </>}
      </div>
      <AssessmentHypotheses assessment={assessment} />
      <AssessmentRefresh assessment={assessment} />
      <AssessmentRisks assessment={assessment} />
      <div className="grid gap-4 border-t pt-4">
        <p className="text-sm font-medium">独立検証の範囲</p>
        <p className="text-xs leading-6 text-muted-foreground">Reviewの存在は、すべての事実の確認や現在の注文資格を示しません。</p>
        <dl className="grid gap-4 sm:grid-cols-2">
          <Field label="一次資料の確認">{SOURCE_CHECK_LABEL[review.primary_source_check]}（{review.primary_source_check}）</Field>
          <Field label="独立検証時刻"><SavedTime value={review.reviewed_at} /></Field>
        </dl>
        <Prose>{review.review_scope}</Prose>
        <div>
          <p className="text-sm font-medium">独立検証の保存確認資料</p>
          {review.checked_source_ids == null || review.checked_sources == null
            ? <p className="text-xs text-muted-foreground">この表示データには確認資料の対応が含まれていません。未確認や確認済みとは補いません。</p>
            : <>
              <p className="text-xs leading-6 text-muted-foreground">検証者が確認対象として記録した当時の資料です。作者の引用や資料全体の一覧とは別で、全記述の検証・資料間の独立性・現在の安全性を保証しません。上の確認範囲と部分確認・不確実性の留保を合わせて読みます。</p>
              {review.checked_source_ids.length === 0
                ? <p className="text-xs text-muted-foreground">この表示データの確認資料は空です。別の引用から補っていません。</p>
                : <AssessmentSources sourceIds={review.checked_source_ids} sources={review.checked_sources} />}
            </>}
        </div>
        {showCountercase && <AssessmentCountercases assessment={assessment} />}
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

function AssessmentValue({ assessment }: { assessment: SavedAssessmentView }) {
  const valuation = assessment.valuation
  const scenarios = valuation?.status === 'resolved'
    ? [{ label: '中心ケース（Base）', value: valuation.base }, { label: '下方ケース（Downside）', value: valuation.downside }]
    : []
  const priceBasis: Record<string, string> = {
    realtime: '観測時の価格（realtime）',
    last_close_unadjusted: '未調整終値',
    last_close_adjusted: '調整済み終値',
  }
  return (
    <section className="grid min-w-0 gap-4 rounded-lg border p-4" aria-label="保存評価の見返り">
      <div>
        <h4 className="text-sm font-semibold">保存評価の見返り</h4>
        <p className="mt-1 text-xs leading-6 text-muted-foreground">評価基準日 <time dateTime={assessment.as_of}>{assessment.as_of}</time> の保存評価です。計算は評価に用いた価格と仮定を固定しています。現在価格・提案指値での再計算ではありません。</p>
      </div>
      {valuation == null ? <p className="text-sm text-muted-foreground">この表示データには評価の見返りが含まれていません。</p> : valuation.status === 'unresolved' ? <div>
        <p className="mb-1 text-sm font-medium">価値評価は未確定</p>
        <Prose>{valuation.reason}</Prose>
      </div> : <>
        <dl className="grid gap-4 sm:grid-cols-3">
          <Field label="評価に用いた株価">{valuation.price_yen} 円</Field>
          <Field label="株価基準日"><time dateTime={valuation.price_as_of}>{valuation.price_as_of}</time></Field>
          <Field label="株価の種類">{valuation.price_basis === null ? '保存値なし' : priceBasis[valuation.price_basis] ?? valuation.price_basis}</Field>
          <Field label="評価時点からの期間">{valuation.horizon_months} か月</Field>
          <Field label="評価で選んだ要求年率">{valuation.required_annual_return_pct} %</Field>
          {valuation.price_observed_at !== null && <Field label="株価の観測時刻"><SavedTime value={valuation.price_observed_at} /></Field>}
        </dl>
        <div className="grid gap-3 sm:grid-cols-2">
          {scenarios.map(({ label, value }) => <div className="min-w-0 rounded-md bg-muted p-4" key={label}>
            <p className="text-sm font-medium">{label}</p>
            <p className="mt-2 text-2xl font-semibold tabular-nums">{formatPct(Number(value.annualized_return_pct), { digits: 2 })}</p>
            <p className="mt-1 text-xs text-muted-foreground">総受取額の年率換算</p>
          </div>)}
        </div>
        <p className="text-xs leading-6 text-muted-foreground">投資家税・費用前、配当再投資なし。期待値・勝率ではなく、下方ケースも最大損失を示しません。</p>
      </>}
      <details className="border-t pt-3">
        <summary className="cursor-pointer text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-4">仮定・計算と最も強い反対論</summary>
        <div className="mt-4 grid min-w-0 gap-5">
          {scenarios.map(({ label, value }) => <div className="grid min-w-0 gap-3" key={label}>
            <p className="text-sm font-medium">{label}の保存前提</p>
            <dl className="grid gap-3 sm:grid-cols-2">
              <Field label="終期価値（1株）">{value.terminal_value_per_share_yen} 円</Field>
              <Field label="期間内の累積分配（1株）">{value.cash_distribution_per_share_yen} 円</Field>
            </dl>
            <Prose>{value.calculation}</Prose>
            {value.source_ids == null || value.sources == null
              ? <p className="text-xs text-muted-foreground">この表示データには、このケースの引用対応が含まれていません。根拠がないことを示しません。</p>
              : <div>
                <p className="text-xs leading-6 text-muted-foreground">このケースの仮定に作者が引用した保存資料です。引用の存在は将来値の確定・予測の的中や独立した支持票を意味しません。</p>
                <AssessmentSources sourceIds={value.source_ids} sources={value.sources} />
              </div>}
          </div>)}
          <AssessmentCountercases assessment={assessment} />
        </div>
      </details>
    </section>
  )
}

function SavedTerms({ terms, withPriceFinding }: { terms: SavedLimitTermsView; withPriceFinding: boolean }) {
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
        {terms.maximum_entry_price_yen !== null && <Field label={withPriceFinding ? "評価と価格条件による保存時の買付上限" : "保存評価の条件付き上限（Pmax）"}>{terms.maximum_entry_price_yen} 円</Field>}
      </dl>
      {terms.maximum_entry_price_yen !== null && <p className="text-xs leading-6 text-muted-foreground">{withPriceFinding ? "保存上限は、査読で裏付けた価格とBaseの算術上限の低い方です。" : "Pmaxは保存評価の条件付き上限です。"}適正価格・勝率・約定価格ではありません。</p>}
      <TextList items={terms.conditions} title="保存条件" />
      <ProposalWarnings warnings={terms.warnings} title="具体条件の保存時の注意事項" />
    </div>
  )
}

function SavedBuyComparison({ item }: { item: SavedBuyItemView }) {
  const comparison = item.saved_buy_comparison
  const sensitivity = comparison?.time_sensitivity
  return <section className="grid min-w-0 gap-4 rounded-lg border p-4" aria-label="保存指値・選択数量の金額比較">
    <h4 className="text-sm font-semibold">保存指値・選択数量の金額比較</h4>
    {comparison == null
      ? <p className="text-sm text-muted-foreground">この表示データには金額比較がありません。旧表示データや価値・具体条件・起点が揃わない場合を、損益0や最大数量で補っていません。</p>
      : <>
        <p className="text-xs leading-6 text-muted-foreground">この銘柄だけが保存指値で全量約定した条件付き比較です。保存時の未拘束cashを共通の元手とし、他の買付・既保有を含むNAVではありません。銘柄間で残cashや終期合計を足し合わせません。</p>
        <dl className="grid gap-4 sm:grid-cols-3">
          <Field label="比較の指値">{comparison.limit_price_yen} 円</Field>
          <Field label="作者が選んだ数量">{comparison.quantity} 株</Field>
          <Field label="保存評価の比較期間">{comparison.horizon_months} か月</Field>
          <Field label="この銘柄への投下額">{comparison.notional_yen} 円</Field>
          <Field label="この銘柄だけを買った後の残cash">{comparison.unspent_cash_yen} 円</Field>
          <Field label="現金維持だけの終期額">{comparison.cash_only_terminal_value_yen} 円</Field>
        </dl>
        <div className="grid gap-3 sm:grid-cols-2">
          {[{ label: '中心ケース（Base）', value: comparison.base }, { label: '下方ケース（Downside）', value: comparison.downside }].map(({ label, value }) => <div className="grid min-w-0 gap-3 rounded-md bg-muted p-4" key={label}>
            <p className="text-sm font-medium">{label}の金額</p>
            <dl className="grid gap-3">
              <Field label="この買付の損益（現金維持との差）"><span className="break-all">{value.profit_loss_yen} 円</span></Field>
              <Field label="この買付の総受取額"><span className="break-all">{value.investment_total_value_yen} 円</span></Field>
              <Field label="残cashを含む終期合計"><span className="break-all">{value.capital_total_value_yen} 円</span></Field>
            </dl>
          </div>)}
        </div>
        <div className="grid min-w-0 gap-3 border-t pt-4">
          <h5 className="text-sm font-medium">保存指値での年率と遅延感応度</h5>
          {sensitivity == null
            ? <p className="text-sm text-muted-foreground">この表示データには指値での時間感応度が未収録です。遅延リスクなしとは判定しません。</p>
            : <>
              <p className="text-xs leading-6 text-muted-foreground">同じ指値・終期価値・累積分配で、比較期間だけを {comparison.horizon_months} か月から {sensitivity.horizon_months} か月へ延ばす条件例です。元評価株価の年率や残cashを含む元手全体の年率ではありません。</p>
              <div className="grid gap-3 sm:grid-cols-2">
                {[{ label: '中心ケース（Base）', value: sensitivity.base }, { label: '下方ケース（Downside）', value: sensitivity.downside }].map(({ label, value }) => <div className="grid min-w-0 gap-3" key={label}>
                  <p className="text-sm font-medium">{label}の時間比較</p>
                  <dl className="grid gap-3">
                    <Field label="保存指値での条件付き年率"><span className="break-all">{value.annualized_return_pct} %</span></Field>
                    <Field label={`${sensitivity.additional_months}か月遅延・総受取額不変の年率`}><span className="break-all">{value.delayed_annualized_return_pct} %</span></Field>
                  </dl>
                </div>)}
              </div>
              <p className="text-xs leading-6 text-muted-foreground">遅延予測・保有期限・新しい評価期間の採択ではありません。損失年率が0へ近づいても上の損失額は減りません。分配の増加や利息、遅延による事業価値・費用の変化は仮定していません。</p>
            </>}
        </div>
        <p className="text-xs leading-6 text-muted-foreground">投資家税・売買費用は含まず、配当再投資なし・cash利息なし。保存仮定の算術で、実績・期待値・最大損失・約定保証ではありません。現在の採否や新たな買付推奨を示しません。</p>
      </>}
  </section>
}

function SavedSellLimitComparison({ item }: { item: SavedSellItemView }) {
  const comparison = item.saved_sell_limit_comparison
  return <section className="grid min-w-0 gap-4 rounded-lg border p-4" aria-label="保存売却下限と保有継続の比較">
    <h4 className="text-sm font-semibold">保存売却下限と保有継続の比較</h4>
    {comparison == null
      ? <p className="text-sm text-muted-foreground">この表示データには下限での比較がありません。旧表示・条件未確定・仮説不成立の売却などを、見返り0や保有継続の支持に置き換えていません。</p>
      : <>
        <p className="text-xs leading-6 text-muted-foreground">作者の売却下限で回収する代わりに、同じ保存価値・今後分配・期間で保有を続ける場合の条件付き試算です。保存終値や購入原価での損益、売却後の運用収益ではありません。</p>
        <dl className="grid gap-4 sm:grid-cols-2">
          <Field label="比較に用いた売却下限">{comparison.calculation_price_yen} 円</Field>
          <Field label="保存評価の比較期間">{comparison.horizon_months} か月</Field>
        </dl>
        <div className="grid gap-3 sm:grid-cols-2">
          {[{ label: '中心ケース（Base）', value: comparison.base }, { label: '下方ケース（Downside）', value: comparison.downside }].map(({ label, value }) => <div className="grid min-w-0 gap-3 rounded-md bg-muted p-4" key={label}>
            <p className="text-sm font-medium">{label}の保有継続</p>
            <dl className="grid gap-3">
              <Field label="終期価値と期間内分配の合計（一株）">{value.total_value_per_share_yen} 円</Field>
              <Field label="売却下限に対する総見返り">{value.total_return_pct} %</Field>
              <Field label="売却下限に対する条件付き年率">{value.annualized_return_pct} %</Field>
            </dl>
          </div>)}
        </div>
        <p className="text-xs leading-6 text-muted-foreground">投資家税・売買費用・配当再投資は含みません。約定・価値実現・現在の採否を保証せず、数値だけで売却や保有へ変更しません。下限引上げ・保有・未約定時の再確認と、保存された作者の理由を併読してください。</p>
      </>}
  </section>
}

function SavedHoldQuoteComparison({ item }: { item: SavedHoldItemView }) {
  const comparison = item.saved_hold_quote_comparison
  return <section className="grid min-w-0 gap-4 rounded-lg border p-4" aria-label="保存終値で確認した保有の見返り">
    <h4 className="text-sm font-semibold">保存終値で確認した保有の見返り</h4>
    {comparison == null
      ? <p className="text-sm text-muted-foreground">この表示データには、保存終値で十分性を確認した比較がありません。旧表示・仮説維持だけの保有・条件不足を、見返り0や十分な収益率へ補っていません。</p>
      : <>
        <p className="text-xs leading-6 text-muted-foreground">保有提案時の終値を機会費用として、同じ保存価値・今後分配・期間で保有を続ける場合の条件付き試算です。元評価の株価での年率、購入原価からの損益や売却できる価格とは別です。</p>
        <dl className="grid gap-4 sm:grid-cols-3">
          <Field label="比較に用いた保存終値">{comparison.calculation_price_yen} 円</Field>
          <Field label="その終値の基準日"><time dateTime={comparison.price_as_of}>{comparison.price_as_of}</time></Field>
          <Field label="保存評価の比較期間">{comparison.horizon_months} か月</Field>
        </dl>
        <div className="grid gap-3 sm:grid-cols-2">
          {[{ label: '中心ケース（Base）', value: comparison.base }, { label: '下方ケース（Downside）', value: comparison.downside }].map(({ label, value }) => <div className="grid min-w-0 gap-3 rounded-md bg-muted p-4" key={label}>
            <p className="text-sm font-medium">{label}の保有継続</p>
            <dl className="grid gap-3">
              <Field label="今後の総受取額（1株）">{value.total_value_per_share_yen} 円</Field>
              <Field label="保存終値に対する総見返り">{value.total_return_pct} %</Field>
              <Field label="保存終値に対する条件付き年率">{value.annualized_return_pct} %</Field>
            </dl>
          </div>)}
        </div>
        <p className="text-xs leading-6 text-muted-foreground">十分性は保存された作者の比較判断です。投資家税・売買費用・配当再投資は含みません。期待値・最大損失や現在の見返りを保証せず、数値だけで保有や売却へ変更しません。</p>
      </>}
  </section>
}

function SavedHoldingPosition({ item }: { item: SavedHoldItemView | SavedSellItemView | SavedDeferItemView }) {
  const position = item.saved_position
  return <section className="grid min-w-0 gap-4 rounded-lg border p-4" aria-label="判断対象の保存保有">
    <h4 className="text-sm font-semibold">判断対象の保存保有</h4>
    {position == null
      ? <p className="text-sm text-muted-foreground">この表示データには対象保有の数量・原価が未収録です。保有なし・0円・確認済みへ補っていません。</p>
      : <>
        <dl className="grid gap-4 sm:grid-cols-2">
          <Field label="保存された保有数量">{position.quantity} 株</Field>
          <Field label="数量の株式単位">{position.quantity_basis_confirmed ? '保存時に確認済み' : '未確認。注文数量として使用しない'}</Field>
          <Field label="保存原価">{position.cost_yen} 円</Field>
          <Field label="入力収集時刻"><time dateTime={position.inputs_checked_at}>{position.inputs_checked_at}</time></Field>
          <Field label="台帳基準時刻"><time dateTime={position.ledger_as_of}>{position.ledger_as_of}</time></Field>
        </dl>
        <p className="text-xs leading-6 text-muted-foreground">提案当時の対象規模です。現在保有・注文可能数量ではなく、後日の売買や株式異動を補っていません。原価は現在時価・回収下限・今後の見返りの分母ではなく、原価だけで保有や売却を決めません。</p>
      </>}
  </section>
}

function HoldingOrigin({ origin }: { origin: SavedHoldingOriginView | null | undefined }) {
  return <details className="rounded-lg border p-4">
    <summary className="cursor-pointer text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-4">当初の投資理由</summary>
    <div className="mt-4 grid gap-4">
      <p className="text-xs leading-6 text-muted-foreground">保存入力に結び付く当初の資料です。今回の評価や再確認条件とは別で、仮説が現在も成立することや保留の解消を意味しません。</p>
      {origin == null
        ? <p className="text-sm text-muted-foreground">この表示データには当初理由が未収録です。理由なし・確認済みへ補っていません。</p>
        : <>
          {origin.assessment !== null && <>
            <AssessmentValueGap assessment={origin.assessment} />
            <AssessmentRealization assessment={origin.assessment} />
            <AssessmentExcerpt assessment={origin.assessment} />
          </>}
          {origin.assessment === null && origin.assessment_ref !== null && <>
            <p className="text-sm text-muted-foreground">保存された当初評価の固定参照はありますが、対応本文を読み取れません。今回や最新の評価では補っていません。</p>
            <Identifier label="当初 assessment_id" value={origin.assessment_ref.assessment_id} />
            <Identifier label="当初 core_sha256" value={origin.assessment_ref.core_sha256} />
            <Identifier label="当初 review_id" value={origin.assessment_ref.review_id} />
          </>}
          {origin.investment_reason !== null && <Prose>{origin.investment_reason}</Prose>}
          {origin.reason_unavailable !== null && <div><p className="mb-1 text-sm font-medium">当初理由を確認できない理由</p><Prose>{origin.reason_unavailable}</Prose></div>}
        </>}
    </div>
  </details>
}

function HoldingReason({ item }: { item: SavedHoldItemView | SavedSellItemView }) {
  return (
    <div className="grid gap-4">
      <p className="text-sm font-medium">{item.action === 'hold'
        ? item.holding_basis === 'investment_case_intact' ? '保有継続の根拠：投資仮説の維持' : '保有継続の根拠：十分な残存見返り'
        : item.reason_code === 'consider_partial_recovery' || item.reason_code === 'consider_full_recovery'
          ? '保有を続ける場合と比べた資金配分の条件案です。売却可能数量・指値・注文は確定していません。'
          : item.reason_code === 'economic_invalidation' ? '売却の根拠：重要な投資仮説の不成立' : '売却の根拠：残存見返りの不足'}</p>
      {item.action === 'hold' && item.holding_basis === 'investment_case_intact' && <p className="text-xs leading-6 text-muted-foreground">投資仮説の維持と、現在の収益率が十分なことは別です。</p>}
      <div><p className="mb-1 text-sm font-medium">今回の再評価理由</p><Prose>{item.case_reassessment_reason}</Prose></div>
      {item.remaining_reward !== null && <div><p className="mb-1 text-sm font-medium">残存見返り：{REWARD_LABEL[item.remaining_reward.status]}</p><Prose>{item.remaining_reward.reason}</Prose></div>}
      {item.action === 'hold' && item.return_calculation_unavailable_reason !== null && <div><p className="mb-1 text-sm font-medium">収益率を計算できない理由</p><Prose>{item.return_calculation_unavailable_reason}</Prose></div>}
      <HoldingOrigin origin={{
        assessment_ref: null,
        assessment: item.original_assessment,
        investment_reason: item.original_investment_reason,
        reason_unavailable: item.original_reason_unavailable_reason,
      }} />
    </div>
  )
}

export function ProposalItemCard({ item }: { item: ProposalItem }) {
  return (
    <SectionCard headingLevel={3} meta={<div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
      <Badge variant="secondary">資金の判断：{proposalActionLabel(item)} · {item.action}</Badge>
      <SavedBuySession item={item} />
    </div>} padded title={item.ticker}>
      <div className="grid min-w-0 gap-5">
        <PriceValueFinding assessment={item.assessment} />
        <ReportSummary text={item.rationale} />
        {item.assessment !== null && <ModelPerspectives assessment={item.assessment} />}
        <details className="rounded-lg border p-4" data-proposal-details>
          <summary className="cursor-pointer text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-4">価格・条件と判断の詳細を読む</summary>
          <div className="mt-5 grid min-w-0 gap-5">
            {item.assessment !== null && <AssessmentValueGap assessment={item.assessment} />}
            {item.assessment !== null && <AssessmentRealization assessment={item.assessment} />}
            {item.assessment !== null && <AssessmentValue assessment={item.assessment} />}
            {item.saved_quote !== null && <dl className="grid gap-4 sm:grid-cols-2">
              <Field label="保存株価（未調整終値）">{item.saved_quote.price_yen} 円</Field>
              <Field label="保存株価の基準日"><time dateTime={item.saved_quote.price_as_of}>{item.saved_quote.price_as_of}</time></Field>
            </dl>}
            {item.action === 'buy' && <p className="text-sm text-muted-foreground">保存時の指値区分：{TIMING_LABEL[item.timing]}（{item.timing}）</p>}
            {(item.action === 'buy' || item.action === 'sell') && (item.terms !== null ? <SavedTerms terms={item.terms} withPriceFinding={item.assessment?.price_value_case != null} /> : <div className="grid gap-3">
              <p className="text-sm font-medium">{item.action === 'sell' && item.reason_code.startsWith('consider_') ? '保有配分の条件案（注文対象外）' : '具体条件未確定'}</p>
              {item.action === 'buy' && item.proposed_limit_yen !== null && <dl><Field label="作者指定の上限（具体条件未確定）">{item.proposed_limit_yen} 円</Field></dl>}
              <Reasons reasons={item.terms_unavailable_reasons} title="具体条件が不足する理由" />
            </div>)}
            {item.action === 'buy' && <SavedBuyComparison item={item} />}
            {item.action === 'sell' && <SavedSellLimitComparison item={item} />}
            {item.action === 'hold' && <SavedHoldQuoteComparison item={item} />}
            {(item.action === 'hold' || item.action === 'sell') && <>
              <SavedHoldingPosition item={item} />
              <HoldingReason item={item} />
            </>}
            {item.action === 'defer' && <>
              <p className="text-sm text-muted-foreground">判断保留は、保有なし・売却済み・保有継続の推奨を意味しません。</p>
              {item.saved_position != null && <>
                <SavedHoldingPosition item={item} />
                <HoldingOrigin origin={item.saved_holding_origin} />
              </>}
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
                  <Field label="ADVの価格窓終端日">{item.liquidity.price_window_end == null ? '保存値なし' : <time dateTime={item.liquidity.price_window_end}>{item.liquidity.price_window_end}</time>}</Field>
                  {item.liquidity.turnover_window != null && <>
                    <Field label="同じ窓の売買代金中央値">{item.liquidity.turnover_window.median_yen === null ? '算出に必要な有効観測数なし' : `${item.liquidity.turnover_window.median_yen} 円`}</Field>
                    <Field label="売買代金の有効観測">{item.liquidity.turnover_window.valid_turnover_count} / {item.liquidity.turnover_window.price_bar_count} 本の価格bar</Field>
                    <Field label="売買代金の価格窓">{item.liquidity.turnover_window.start_date} ～ {item.liquidity.turnover_window.end_date}</Field>
                  </>}
                </dl>
                {item.liquidity.turnover_window == null && <p className="text-sm text-muted-foreground">売買代金窓の本数・中央値は保存値なし。</p>}
                <p className="text-sm text-muted-foreground">実在価格barの窓であり、共通営業日数とは限りません。欠落日を無出来高0に補完していません。</p>
                <p className="text-sm text-muted-foreground">価格窓の終端であり、売買代金の最終有効日や鮮度の保証ではありません。</p>
                {item.liquidity.unavailable_reason !== null && <Prose>{item.liquidity.unavailable_reason}</Prose>}
                <p className="text-xs leading-6 text-muted-foreground">保存値であり、現在の板や約定可能性を示しません。</p>
              </div>
            </details>}
            {item.assessment === null ? <p className="text-sm text-muted-foreground">未評価：この項目には保存評価への参照がありません。</p> : <details className="rounded-lg border p-4">
              <summary className="cursor-pointer text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-4">今回の保存評価・独立検証の抜粋</summary>
              <div className="mt-4"><AssessmentExcerpt assessment={item.assessment} showCountercase={false} /></div>
            </details>}
          </div>
        </details>
      </div>
    </SectionCard>
  )
}

function SavedCash({ cash }: { cash: SavedProposalView['saved_cash'] }) {
  return <section className="grid gap-3 rounded-lg border p-4" aria-label="比較の出発点となる保存cash">
    <h3 className="text-sm font-semibold">比較の出発点となる保存cash</h3>
    {cash == null
      ? <p className="text-sm text-muted-foreground">この表示データには未拘束cashと確認時刻がありません。0円や全体資本から補っていません。</p>
      : <>
        <dl className="grid gap-4 sm:grid-cols-3">
          <Field label="保存時の未拘束cash">{cash.available_cash_yen} 円</Field>
          <Field label="cash入力の収集時刻"><SavedTime value={cash.inputs_checked_at} /></Field>
          <Field label="cashの台帳基準時刻"><SavedTime value={cash.ledger_as_of} /></Field>
        </dl>
        <p className="text-xs leading-6 text-muted-foreground">保存提案全体に共通する比較前の未拘束額です。総資産や買付後の残cashではなく、銘柄別に加算しません。既存の資金拘束は控除済みなので再控除せず、予定入金・未約定売却代金や現在残高を補っていません。全額投資・現金維持の推奨や現在の注文可能額ではありません。</p>
      </>}
  </section>
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

export function ProposalReport({ data, ticker, above }: { data: LatestProposalView | null; ticker?: string; above?: ReactNode }) {
  if (data === null) return <LoadingPage label="売買提案を読み込んでいます" />
  if (data.status === 'unavailable') return <PageState message={data.error ?? '保存済み提案を読み取れませんでした。'} title="売買提案の読取不能" />
  if (data.status === 'empty') return <PageShell lead="最新基準日の保存済み提案を読みます。" meta={<span className="text-xs text-muted-foreground">表示データ生成 <SavedTime value={data.generated_at} /></span>} title="売買提案" width="reading"><SectionCard padded title="保存済み提案はありません"><Prose>表示データ生成時点で、正式に保存された提案はありません。</Prose></SectionCard></PageShell>
  const proposal = data.proposal
  if (proposal === null) return <PageState message="保存済み提案を読み取れませんでした。" title="売買提案の読取不能" />
  const selected = ticker === undefined ? null : proposal.items.find(item => item.ticker === ticker)
  if (selected === undefined) return <PageState message="指定された銘柄はこの保存提案にありません。" title="レポートを表示できません" />
  const items = selected === null ? proposal.items : [selected]
  return (
    <PageShell above={above} lead={selected === null ? '保存済み提案を、保存順のまま表示します。' : `${selected.ticker} · ${selected.sector ?? '業種未記録'} · ${formatJstDateTime(proposal.published_at)} JST 発行`} meta={<Badge variant="outline">{SCOPE_LABEL[proposal.request_scope]}</Badge>} title={selected === null ? "売買提案" : (selected.company_name ?? selected.ticker)} width="reading">
      {selected !== null && <p className="text-xs leading-6 text-muted-foreground">保存時点のレポートです。現在の注文条件の再確認・採否・発注は別です。</p>}
      {selected === null && <SectionCard padded title="サマリ">
        <div className="grid gap-4">
          <ReportSummary text={proposal.headline} />
          <p className="text-xs leading-6 text-muted-foreground">保存時点の提案です。現在の注文条件の再確認・採否・発注は別です。</p>
          <details className="border-t pt-3">
            <summary className="cursor-pointer text-sm text-muted-foreground focus-visible:outline-2 focus-visible:outline-offset-4">提案の保存日時</summary>
            <div className="mt-4 grid gap-4">
              <dl className="grid gap-4 sm:grid-cols-2">
                <Field label="提案基準日"><time dateTime={proposal.as_of}>{proposal.as_of}</time></Field>
                <Field label="提案本文の準備時刻"><SavedTime value={proposal.prepared_at} /></Field>
                <Field label="提案の発行時刻"><SavedTime value={proposal.published_at} /></Field>
                <Field label="表示データ生成時刻"><SavedTime value={data.generated_at} /></Field>
              </dl>
              <Identifier key={proposal.proposal_id} label="proposal_id" value={proposal.proposal_id} />
              <p className="text-xs leading-6 text-muted-foreground">表示データ生成時点の保存集合で、提案基準日・発行時刻・提案IDの順に最新の1件です。現在の注文可否は判定していません。対象取引日は実注文の期限ではありません。</p>
            </div>
          </details>
        </div>
      </SectionCard>}
      <section aria-labelledby="proposal-items-heading" className="grid min-w-0 gap-4">
        {(proposal.corrections ?? []).length > 0 && <SectionCard padded title="提案履歴の訂正">
          {(proposal.corrections ?? []).map((correction) => <div key={correction.correction_id} className="grid gap-2">
            <Prose>{correction.reason}</Prose>
            <p>{isCorrectedProposal(proposal) ? "表示中は訂正済みの旧提案です。採用候補ではありません。" : "誤って保存した提案を最新候補から除外しました。"}表示中の提案の根拠・条件・発行時刻は当時のままです。現在の再推奨や人間の採用ではありません。</p>
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
        {items.map((item) => <ProposalItemCard item={item} key={`${proposal.proposal_id}-${item.ticker}`} />)}
      </section>
      <details className="rounded-lg border bg-card p-5 sm:p-6">
        <summary className="cursor-pointer text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-4">全体の比較・見送りと注意事項</summary>
        <div className="mt-5 grid gap-5">
          {selected !== null && <div className="grid gap-3">
            <p className="text-sm font-medium">この銘柄を含む保存提案のサマリ</p><Prose>{proposal.headline}</Prose>
            <dl className="grid gap-4 sm:grid-cols-2">
              <Field label="提案基準日"><time dateTime={proposal.as_of}>{proposal.as_of}</time></Field>
              <Field label="提案本文の準備時刻"><SavedTime value={proposal.prepared_at} /></Field>
              <Field label="提案の発行時刻"><SavedTime value={proposal.published_at} /></Field>
              <Field label="表示データ生成時刻"><SavedTime value={data.generated_at} /></Field>
            </dl>
            <Identifier label="proposal_id" value={proposal.proposal_id} />
          </div>}
          <SavedCash cash={proposal.saved_cash} />
          <SavedMarketContext context={proposal.saved_market_context} />
          <div><p className="mb-2 text-sm font-medium">全体の比較</p><Prose>{proposal.comparison}</Prose></div>
          <div><p className="mb-2 text-sm font-medium">採らなかった案・機会損失</p><Prose>{proposal.forgone}</Prose></div>
          <ProposalWarnings warnings={proposal.warnings} title="提案の保存時の注意事項" />
        </div>
      </details>
      <PortfolioExposure exposure={proposal.portfolio_exposure} />
    </PageShell>
  )
}
