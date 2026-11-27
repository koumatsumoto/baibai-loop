import type { SavedModelPerspectiveView } from '../api/types'

export const PERSPECTIVES_TITLE = '売買判断の7つの観点'

export const REPORT_MODELS: readonly { model: SavedModelPerspectiveView['model']; label: string }[] = [
  { model: 'company_financials', label: '企業財務' },
  { model: 'company_growth', label: '企業成長' },
  { model: 'market_context', label: '市場環境' },
  { model: 'external_assessment', label: '外部評価' },
  { model: 'technical_analysis', label: 'テクニカル' },
  { model: 'industry_trends', label: '産業動向' },
  { model: 'social_trends', label: '社会動向' },
]

export const PERSPECTIVE_JUDGMENTS: Record<SavedModelPerspectiveView['assessment'], { mark: string; label: string; style: string }> = {
  supportive: { mark: '＋', label: '支持', style: 'bg-emerald-500/10 text-emerald-700' },
  mixed: { mark: '±', label: '支持・相反', style: 'bg-amber-500/10 text-amber-800' },
  adverse: { mark: '−', label: '逆風', style: 'bg-rose-500/10 text-rose-700' },
  neutral: { mark: '＝', label: '中立', style: 'bg-muted text-muted-foreground' },
  insufficient: { mark: '？', label: '情報不足', style: 'bg-muted text-muted-foreground' },
  not_applicable: { mark: '／', label: '対象外', style: 'bg-muted text-muted-foreground' },
}
