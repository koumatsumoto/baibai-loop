import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { SavedModelPerspectiveView, SavedAssessmentView } from '../src/api/types'
import { ModelPerspectives } from '../src/components/ModelPerspectives'
import { assessmentFixture } from './fixtures/proposals'

function render(assessment: SavedAssessmentView) {
  return renderToStaticMarkup(createElement(ModelPerspectives, { assessment }))
}

describe('モデル別の保存判断', () => {
  it('七観点を一定順序で表示し、未記録を推測で埋めない', () => {
    const html = render(assessmentFixture('legacy'))
    for (const label of ['企業財務', '企業成長', '市場環境', '外部評価', 'テクニカル分析', '産業動向', '社会動向']) expect(html).toContain(label)
    expect(html.match(/scope="row"/g)).toHaveLength(7)
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
  expect(financialRow.match(/sr-only/g)).toHaveLength(1)
  expect(financialRow).toContain('>支持<')
  expect(chart).toContain('colSpan="4"')
  expect(chart).toContain('>情報不足<')
  expect(chart).not.toContain(assessment.model_perspectives[0].summary)
  expect(html.slice(html.indexOf('<details'))).toContain(assessment.model_perspectives[0].summary)
  expect(html).not.toMatch(/<details[^>]*\bopen(?:=|\s|>)/)
  expect(html).not.toContain('総合点')
  expect(html).not.toContain('勝率')
})
