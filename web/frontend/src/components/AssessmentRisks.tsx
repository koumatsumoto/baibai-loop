import type { SavedPermanentLossRiskView, SavedAssessmentView } from '../api/types'
import { AssessmentSources } from './AssessmentSources'

const AXES: Record<SavedPermanentLossRiskView['axis'], string> = {
  funding_liquidity: '資金余力', debt_repayment: '借入返済', cash_flow: 'キャッシュフロー',
  dilution: '希薄化', customer_concentration: '顧客集中', structural_decline: '構造的衰退',
  governance_accounting: 'ガバナンス・会計',
}
const JUDGMENTS: Record<SavedPermanentLossRiskView['assessment'], string> = {
  acceptable: '受容可能', adverse: '不利な要因', unknown: '不明',
}
const EVIDENCE: Record<SavedPermanentLossRiskView['evidence_status'], string> = {
  verified: '確認済み', partially_verified: '部分確認', unverified: '未確認',
}

export function AssessmentRisks({ assessment }: { assessment: SavedAssessmentView }) {
  const analysis = assessment.risk_analysis
  return <section className="grid min-w-0 gap-3 rounded-lg border p-4" aria-label="リスクと未解決の問い">
    <div>
      <h4 className="text-sm font-semibold">リスクと未解決の問い</h4>
      <p className="mt-1 text-xs leading-6 text-muted-foreground">評価基準日 <time dateTime={assessment.as_of}>{assessment.as_of}</time> の保存検討です。確認済みは記した根拠の確認範囲で、安全保証や現在の購入可否ではありません。問いの有無で自動判定せず、引受理由と反対論を併せて確認してください。</p>
    </div>
    {analysis == null ? <p className="text-sm text-muted-foreground">この表示データにはリスク・未解決の問いが含まれていません。検討していない、またはリスクがないという意味ではありません。</p> : <>
      <h5 className="text-sm font-medium">永久損失の検討</h5>
      {analysis.risks.length === 0 ? <p className="text-sm text-muted-foreground">保存された項目はありません。安全という判定ではありません。</p> : <ul className="grid gap-4">
        {analysis.risks.map(risk => <li className="min-w-0 border-b pb-4 last:border-0 last:pb-0" key={risk.axis} data-risk-axis={risk.axis}>
          <p className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm"><span className="font-medium">{AXES[risk.axis]}</span><span>{JUDGMENTS[risk.assessment]}（{risk.assessment}）</span></p>
          <p className="mt-1 text-xs leading-6 text-muted-foreground">根拠の確認：{EVIDENCE[risk.evidence_status]} · 検討基準日 <time dateTime={risk.as_of}>{risk.as_of}</time></p>
          <p className="mt-2 whitespace-pre-wrap break-words text-sm leading-7">{risk.summary}</p>
          <AssessmentSources sourceIds={risk.source_ids} sources={analysis.sources} />
        </li>)}
      </ul>}
      <h5 className="mt-2 text-sm font-medium">評価に残った問い</h5>
      {analysis.unresolved_questions.length === 0 ? <p className="text-sm text-muted-foreground">この評価に未解決の問いは記録されていません。将来の不確実性がないことを示しません。</p> : <ul className="grid list-disc gap-2 pl-5 text-sm leading-7">
        {analysis.unresolved_questions.map((question, index) => <li className="whitespace-pre-wrap break-words" key={index}>{question}</li>)}
      </ul>}
    </>}
  </section>
}
