import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { ExternalAssessment } from '../src/api/types'
import { ExternalAssessmentSection } from '../src/pages/stocks/ExternalAssessmentSection'
import fixture from './fixtures/external-assessment.json'

function render(assessment: ExternalAssessment | null) {
  return renderToStaticMarkup(createElement(ExternalAssessmentSection, { assessment }))
}

describe('外部参考評価の表示', () => {
  it('媒体の異なる提示値と尺度、取得日・基準日、未確認basisを分ける', () => {
    const html = render(fixture as ExternalAssessment)
    expect(html).toContain('外部モデル価格')
    expect(html).toContain('アナリスト rating')
    expect(html).toContain('Quants Research score')
    expect(html).toContain('TradingView · 外部予測欄')
    expect(html).toContain('株式basis 未確認')
    expect(html).toContain('提示基準日 2026-10-09')
    expect(html).toContain('取得(UTC)')
    expect(html).toContain('予測の通貨 未確認')
    expect(html).toContain('独立票として合算しません')
    expect(html).toContain('>0</td>')
    expect(html).toContain('比較基準が未確認・変化')
    expect(html).toContain('行単位の対象期と各予測指標の対応が未確認')
    expect(html).toContain('数値差分の比較は未対応')
  })

  it('全nullの受領行と未解決行を予測ありへ変えない', () => {
    const assessment = structuredClone(fixture) as ExternalAssessment
    assessment.tradingview.availability = 'all_null'
    assessment.tradingview.forecast_fields = []
    expect(render(assessment)).toContain('受領済みですが、予測欄には値がありません')
    assessment.tradingview.availability = 'unresolved'
    expect(render(assessment)).toContain('応答内で未解決と記録された行')
  })

  it('未取得を明示する', () => {
    expect(render(null)).toContain('保存済み外部観測なし')
  })
})
