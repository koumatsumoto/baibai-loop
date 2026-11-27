import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { EstimatedPerspectiveView, SavedModelPerspectiveView, SavedAssessmentView } from '../src/api/types'
import { ModelPerspectives } from '../src/components/ModelPerspectives'
import { ProposalItemCard } from '../src/pages/ProposalPage'
import { assessmentFixture, unassessedDeferFixture } from './fixtures/proposals'

function render(assessment: SavedAssessmentView) {
  return renderToStaticMarkup(createElement(ModelPerspectives, { assessment }))
}

describe('モデル別の保存判断', () => {
  it('七観点を一定順序で表示し、未記録を推測で埋めない', () => {
    const html = render(assessmentFixture('legacy'))
    const labels = [...html.matchAll(/<th[^>]*scope="row"[^>]*>([^<]+)<\/th>/g)].map(match => match[1])
    expect(labels).toEqual(['企業財務', '企業成長', '市場環境', '外部評価', 'テクニカル', '産業動向', '社会動向'])
    expect(html).toMatch(/<h5[^>]*>テクニカル<span/)
    expect(html.slice(html.indexOf('<tbody>'), html.indexOf('</tbody>')).match(/>未記録</g)).toHaveLength(7)
    const chartRows = html.slice(html.indexOf('<tbody>'), html.indexOf('</tbody>'))
    expect(chartRows).not.toContain('>中立<')
    expect(chartRows).not.toContain('>支持<')
    expect(chartRows).not.toContain('>情報不足<')
    expect(html).not.toContain('0 / 7')
    expect(html).not.toContain('総合点')
  })

  it('先行配備されたUIでも旧表示JSONの省略を未記録として読む', () => {
    const old = assessmentFixture('old')
    Reflect.deleteProperty(old, 'model_perspectives')
    Reflect.deleteProperty(old, 'model_perspective_sources')
    expect(render(old)).toContain('観点別の判断は保存されていません。')
  })

  it('混在・情報不足・対象外と、判断を保存していない観点を分ける', () => {
    const assessment = assessmentFixture('mixed')
    assessment.model_perspectives = [
      { model: 'company_financials', assessment: 'mixed', summary: '報告利益は改善。継続事業の利益との比較は不一致。', source_ids: ['ir'] },
      { model: 'industry_trends', assessment: 'insufficient', summary: '原本あり、対象業界の意味抽出は未対応。', source_ids: [] },
      { model: 'social_trends', assessment: 'not_applicable', summary: '今回確認した制度変更は対象事業に適用されない。', source_ids: ['ir'] },
    ]
    const html = render(assessment)
    expect(html).toContain('>支持・相反<')
    expect(html).toContain('>情報不足<')
    expect(html).toContain('>対象外<')
    expect(html.slice(html.indexOf('<tbody>'), html.indexOf('</tbody>')).match(/>未記録</g)).toHaveLength(4)
    for (const row of assessment.model_perspectives) expect(html).toContain(row.summary)
    expect(html).toContain('参照先の表示情報なし')
    expect(html).not.toContain('変化なし')
  })

  it.each([
    ['supportive', '支持'], ['mixed', '支持・相反'], ['adverse', '逆風'], ['neutral', '中立'],
  ] as const)('色だけに依存せず判断の意味を文字で残す: %s', (judgment, label) => {
    const assessment = assessmentFixture('meaning')
    assessment.model_perspectives = [{ model: 'technical_analysis', assessment: judgment, summary: '価格窓だけで価値は判定しない。', source_ids: ['price'] } satisfies SavedModelPerspectiveView]
    expect(render(assessment)).toContain(`>${label}<`)
  })

  it('ライト固定の背景にOSのdark設定だけで淡色文字を適用しない', () => {
    const assessment = assessmentFixture('light-palette')
    assessment.model_perspectives = [
      { model: 'company_financials', assessment: 'supportive', summary: '根拠のある支持。', source_ids: ['source'] },
      { model: 'company_growth', assessment: 'mixed', summary: '支持と相反が併存。', source_ids: ['source'] },
      { model: 'technical_analysis', assessment: 'adverse', summary: '価格面の逆風。', source_ids: ['source'] },
    ]
    const html = render(assessment)
    for (const color of ['text-emerald-700', 'text-amber-800', 'text-rose-700']) expect(html).toContain(color)
    expect(html).not.toContain('dark:')
  })

  it('exact根拠の対象日と取得時刻を評価日へ置換せず、本文とURLをescapeする', () => {
    const assessment = assessmentFixture('clocks')
    assessment.model_perspectives = [{ model: 'company_growth', assessment: 'mixed', summary: '<script>alert(1)</script>\n実績成長と予想を区別。', source_ids: ['old-ir', 'saved'] }]
    assessment.model_perspective_sources = [
      { source_id: 'old-ir', source_tier: 'primary', ref: 'https://example.com/report?date=2026-09-30&x=1', provider: null, dataset: null, as_of: '2026-09-30', retrieved_at: '2026-10-20T03:00:00Z', used_for: '四半期実績。将来成長ではない。' },
      { source_id: 'saved', source_tier: 'local_data', ref: null, provider: 'synthetic', dataset: 'fixed-generation', as_of: '2026-10-19', retrieved_at: '2026-10-21T03:00:00Z', used_for: '固定世代の保存値。現在値でない。' },
    ]
    const html = render(assessment)
    expect(html).toContain('dateTime="2026-10-26"')
    expect(html).toContain('根拠の対象日：<time dateTime="2026-09-30"')
    expect(html).toContain('取得時刻：<time dateTime="2026-10-20T03:00:00Z"')
    expect(html).toContain('保存データ：synthetic / fixed-generation')
    expect(html).toContain('href="https://example.com/report?date=2026-09-30&amp;x=1"')
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;\n実績成長と予想を区別。')
    expect(html).not.toContain('<script>')
  })
})

it('分類チャートには一観点につき一判断を置き、説明と出典は閉じた詳細へ残す', () => {
  const assessment = assessmentFixture('chart')
  assessment.model_perspectives = [
    { model: 'company_financials', assessment: 'supportive', summary: '本文は折りたたみ内にだけ置く。', source_ids: [] },
    { model: 'company_growth', assessment: 'mixed', summary: '成長と希薄化を併せて検討。', source_ids: [] },
    { model: 'technical_analysis', assessment: 'insufficient', summary: '価格窓が足りない。', source_ids: [] },
  ]
  const html = render(assessment)
  const chart = html.slice(0, html.indexOf('<details'))
  const financialRow = chart.split('data-model="company_financials"')[1].split('</tr>')[0]
  expect(financialRow).toMatch(/data-evidence="author"[^>]*>.*>支持<\/span>/)
  expect(financialRow).not.toContain('data-evidence="observation"')
  expect(chart).toContain('当時の作者判断')
  expect(chart).toContain('固定資料の観測')
  expect(chart).toContain('>情報不足<')
  expect(chart).not.toContain(assessment.model_perspectives[0].summary)
  expect(html).not.toContain('提案時の固定資料を、後から表示用に読み取った観測です。')
  expect(html.slice(html.indexOf('<details'))).toContain(assessment.model_perspectives[0].summary)
  expect(html).not.toMatch(/<details[^>]*\bopen(?:=|\s|>)/)
  expect(html).not.toContain('総合点')
  expect(html).not.toContain('勝率')
})

it('当時の資料による見立てを区別し、元の不足判断を推定で置き換えない', () => {
  const a = assessmentFixture('supplement')
  a.model_perspectives = [{ model: 'company_growth', assessment: 'insufficient', summary: '元の作者が確認できず。', source_ids: [] }]
  a.estimated_model_chart = {
    monex_fiscal_eps: null,
    selection_sha256: 'a'.repeat(64), input_known_at: '2026-10-26T03:00:00Z', research_as_of: '2026-10-26', generation_id: 'fixed-test',
    rows: [
      { model: 'company_growth', assessment: 'supportive', summary: '上書きしてはいけない', evidence: '古い選定', observed_on: '2026-09-30', source_url: null },
      { model: 'social_trends', assessment: 'context_only', summary: '観測された金融条件。個社影響は未確定。', evidence: '当時の記事', observed_on: '2026-09-18', source_url: 'https://www.boj.or.jp/' },
      { model: 'market_context', assessment: 'mixed', summary: '短期と長期で方向が逆。', evidence: '当時の市場代理', observed_on: '2026-10-23', source_url: null },
    ],
  }
  const html = render(a)
  expect(html).not.toContain('上書きしてはいけない')
  expect(html).toContain('元の作者が確認できず。')
  expect(html).toContain('2026-10-26 12:00')
  expect(html).toContain('2026-09-18')
  expect(html).toContain('元資料を開く')
  const rows = html.split('<tbody>')[1].split('</tbody>')[0]
  expect(rows.split('data-model="social_trends"')[1].split('</tr>')[0]).toContain('観測あり・個社への影響未判定')
  const growth = rows.split('data-model="company_growth"')[1].split('</tr>')[0]
  expect(growth).toContain('data-evidence="author"')
  expect(growth).not.toContain('data-evidence="observation"')
  const market = rows.split('data-model="market_context"')[1].split('</tr>')[0]
  expect(market).toMatch(/data-perspective="author"[^>]*>.*>未記録<.*<\/td>/)
  expect(market).toMatch(/data-evidence="observation"[^>]*>両方向<\/span>/)
  expect(market).not.toContain('>支持')
  expect(html).toContain('dateTime="2026-10-23"')
  expect(html).toContain('提案時の固定資料を、後から表示用に読み取った観測です。')
  expect(html).toContain('dateTime="2026-10-26T03:00:00Z"')
  expect(html.slice(0, html.indexOf('<table'))).toContain('投資仮説への支持・反証を意味しません')
  a.model_perspectives = []
  const estimatedOnly = render(a).split('<tbody>')[1].split('</tbody>')[0]
  expect(estimatedOnly).toMatch(/data-evidence="observation"[^>]*>プラス側<\/span>/)
  expect(estimatedOnly).not.toContain('data-evidence="author"')
})

it.each([
  ['adverse', '逆風'], ['mixed', '支持・相反'], ['supportive', '支持'],
] as const)('市場代理2窓のマイナスを当時の作者判断と混ぜない: %s', (judgment, label) => {
  const a = assessmentFixture('market-clock')
  const summary = '市場代理1321の終値・20本前比 -2.5%、市場代理1321の終値・60本前比 -13.0%。'
  a.estimated_model_chart = {
    selection_sha256: 'f'.repeat(64), input_known_at: '2026-10-26T03:00:00Z',
    research_as_of: '2026-10-26', generation_id: 'fixed-market', monex_fiscal_eps: null,
    rows: [{ model: 'market_context', assessment: 'context_only', summary,
      evidence: '市場環境：market.points', observed_on: '2026-10-23', source_url: null }],
  }
  const tickerRow = (html: string) => html.split('data-model="market_context"')[1].split('</tr>')[0]
  const historical = render(a)
  expect(tickerRow(historical)).toContain('観測あり・個社への影響未判定')
  expect(tickerRow(historical)).not.toContain('data-evidence="author"')
  expect(historical).toContain(summary)
  expect(historical).toContain('dateTime="2026-10-23"')
  expect(historical).toContain('dateTime="2026-10-26T03:00:00Z"')
  a.model_perspectives = [{ model: 'market_context', assessment: judgment, summary: '当時の個社・同業との比較', source_ids: [] }]
  const reviewed = render(a)
  expect(tickerRow(reviewed)).toContain('data-evidence="author"')
  expect(tickerRow(reviewed)).toContain(`>${label}<`)
  expect(tickerRow(reviewed)).not.toContain('data-evidence="observation"')
  expect(reviewed).toContain('当時の個社・同業との比較')
})

it('合成対照で49明細の作者105・固定観測238を保ち、評価なし2を作らない', () => {
  const models: SavedModelPerspectiveView['model'][] = [
    'company_financials', 'company_growth', 'market_context', 'external_assessment',
    'technical_analysis', 'industry_trends', 'social_trends',
  ]
  let observation = 0
  const charts = Array.from({ length: 49 }, (_, index) => {
    const a = assessmentFixture(`coverage-${index}`)
    const part = models.slice(0, index < 15 ? 7 : 0)
    a.model_perspectives = part.map(model => ({
      model, assessment: 'insufficient', summary: '合成の作者原記録', source_ids: [],
    }))
    a.estimated_model_chart = {
      selection_sha256: 'a'.repeat(64), input_known_at: '2026-10-26T03:00:00Z',
      research_as_of: '2026-10-26', generation_id: 'synthetic-coverage', monex_fiscal_eps: null,
      rows: models.slice(part.length).map(model => {
        const index = observation++
        const assessment: EstimatedPerspectiveView['assessment'] = index === 232 ? 'insufficient'
          : index < 66 ? 'supportive' : index < 95 ? 'mixed' : index < 131 ? 'adverse' : 'context_only'
        return {
          model, assessment, summary: index === 232 ? '会計基準不一致で前年と比較できない。' : `合成の固定観測 ${index}`,
          evidence: '合成の同じ資料',
          observed_on: '2026-10-23', source_url: null,
        }
      }),
    }
    const html = render(a)
    expect(html.match(/scope="row"/g)).toHaveLength(7)
    return html
  }).join('')
  expect(charts.match(/data-evidence="author"/g)).toHaveLength(105)
  expect(charts.match(/data-evidence="observation"/g)).toHaveLength(238)
  expect(charts.match(/data-evidence="observation"[^>]*>プラス側<\/span>/g)).toHaveLength(66)
  expect(charts.match(/data-evidence="observation"[^>]*>両方向<\/span>/g)).toHaveLength(29)
  expect(charts.match(/data-evidence="observation"[^>]*>マイナス側<\/span>/g)).toHaveLength(36)
  expect(charts.match(/data-evidence="observation"[^>]*>観測あり・個社への影響未判定<\/span>/g)).toHaveLength(106)
  expect(charts.match(/data-evidence="observation"[^>]*>情報不足<\/span>/g)).toHaveLength(1)
  expect(charts).toContain('会計基準不一致で前年と比較できない。')
  assertNoAssessment('1001')
  assertNoAssessment('1002')
})

function assertNoAssessment(ticker: string) {
  const html = renderToStaticMarkup(createElement(ProposalItemCard, {
    item: { ...unassessedDeferFixture, ticker },
  }))
  expect(html).toContain(ticker)
  expect(html).not.toContain('7モデルの観点')
  expect(html).not.toContain('data-evidence="observation"')
}

it('外部評価の当初判断は変えず、今期・来期EPSだけを受領時刻付きで併記する', () => {
  const a = assessmentFixture('fiscal-eps')
  a.model_perspectives = [{ model: 'external_assessment', assessment: 'insufficient', summary: '当初の判定を保持', source_ids: [] }]
  const fiscal = { provider_label: '今期予想コンセンサス', fiscal_end_label: '2025/09', eps_jpy_per_share: -3.5, analyst_count: null, provider_published_at_utc: null }
  a.estimated_model_chart = {
    selection_sha256: 'fixed', generation_id: 'fixed', input_known_at: '2025-03-04T06:00:00Z', research_as_of: '2025-03-04',
    rows: [], monex_fiscal_eps: { source_url: 'https://scouter.monex.co.jp/report/theoryDps/1234', received_at_utc: '2025-03-04T05:40:00Z',
      current_fy: fiscal, next_fy: { ...fiscal, provider_label: '来期予想コンセンサス', fiscal_end_label: '2026/09', eps_jpy_per_share: 0 },
      annualized_to_12_months: true, share_basis: 'shares_at_provider_last_update',
    },
  }
  const html = render(a)
  const chart = html.split('<tbody>')[1].split('</tbody>')[0]
  const external = chart.split('data-model="external_assessment"')[1].split('</tr>')[0]
  expect(external).toContain('情報不足')
  for (const wording of ['当初の判定を保持', '2025/09期', '-3.5 円/株', '2026/09期', '0 円/株', '配信元：マネックス証券', '保存ページの受領', '公表日時は未確認']) expect(html).toContain(wording)
  a.estimated_model_chart.monex_fiscal_eps!.current_fy = null
  expect(render(a)).toContain('未確認')
  expect(render(a)).not.toContain('-3.5 円/株')
})
