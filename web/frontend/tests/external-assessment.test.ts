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

  it('楽天の保存観測なしを他媒体の値や正常nullと混同しない', () => {
    const assessment = structuredClone(fixture) as ExternalAssessment
    const source = assessment.analyst_ratings[0]
    source.inputs = { latest: null, previous: null }
    source.comparison = { ...source.comparison, state: 'not_observed', reason: 'no_observation', latest_value: null, previous_value: null, delta: null, comparable: false }
    const html = render(assessment)
    expect(html).toContain('rakuten · アナリスト rating')
    expect(html).toContain('欠測理由は未確認')
    expect(html).toContain('評価の不存在・中立評価を意味しません')
    expect(html).not.toContain('正常null')
    expect(html).toContain('目標株価コンセンサス')
    assessment.model_valuations = []
    assessment.quants_scores = []
    assessment.tradingview.inputs.latest = null
    expect(render(assessment)).toContain('この対象時点以前の保存済み外部評価はありません')
  })

  it('楽天の正常nullは受領時刻とともに表示し、古い値を現在値にしない', () => {
    const assessment = structuredClone(fixture) as ExternalAssessment
    const source = assessment.analyst_ratings[0]
    const row = source.inputs.latest!
    row.rating = null
    source.comparison = { ...source.comparison, state: 'missing', reason: 'latest_missing', latest_value: null, delta: null, comparable: false }
    const html = render(assessment)
    expect(html).toContain('rating値は欠測（正常null）')
    expect(html).toContain(row.fetched_at_utc)
    expect(html).not.toContain('欠測理由は未確認')
    row.rating = 0
    source.comparison.latest_value = 0
    expect(render(assessment)).toContain('>0</td>')
    expect(render(assessment)).toContain('現在値の保証ではありません')
  })
})

// 全て合成値。原HTML、個人の保有・評価本文は使わない。
function minkabuOnly(available: boolean): ExternalAssessment {
  const assessment = structuredClone(fixture) as ExternalAssessment
  assessment.model_valuations = []
  assessment.analyst_ratings = []
  assessment.quants_scores = []
  assessment.origin_groups = []
  assessment.tradingview.inputs = { latest: null, previous: null }
  assessment.minkabu = { previous: null, latest: {
    ticker: '9999', fetched_at_utc: '2026-10-10T07:00:00Z', snapshot_date: '2026-10-10',
    distributor: 'minkabu', origin: null,
    source_url: available ? 'https://minkabu.jp/stock/9999/analyst_consensus' : 'https://minkabu.jp/stock/9999',
    parser_version: 'synthetic-minkabu-review-1', contract_version: '1',
    availability: available ? 'available' : 'not_covered',
    display_as_of_date: available ? '2026-10-09' : null,
    rating_label: null, strong_buy_count: null, buy_count: null, neutral_count: null,
    sell_count: null, strong_sell_count: null,
    target_price_yen: available ? 1500 : null, target_horizon_months: null,
    forecast_period_label: null, analyst_forecast_as_of_date: null,
    company_forecast_as_of_date: null, analyst_revenue_million_yen: null,
    analyst_net_income_million_yen: null, analyst_eps_yen: available ? 0 : null,
    company_revenue_million_yen: null, company_net_income_million_yen: null,
    company_eps_yen: null, share_basis: null,
  } }
  return assessment
}

describe('Minkabuの受領状態', () => {
  it('他媒体なしでも保存予想と受領時刻を表示する', () => {
    const html = render(minkabuOnly(true))
    expect(html).not.toContain('この対象時点以前の保存済み外部評価はありません')
    expect(html).toContain('みんかぶ')
    expect(html).toContain('1,500')
    expect(html).toContain('2026-10-10T07:00:00Z')
    expect(html).toContain('表示基準日 2026-10-09')
    expect(html).toContain('原評価元 未確認')
    expect(html).toContain('株式basis 未確認')
    expect(html).toContain('現在値の保証ではありません')
    expect(html).toMatch(/EPS（円）<\/td><td[^>]*>0<\/td><td[^>]*>—<\/td>/)
  })

  it('明示的な対象外も未取得へ変えない', () => {
    const html = render(minkabuOnly(false))
    expect(html).not.toContain('この対象時点以前の保存済み外部評価はありません')
    expect(html).toContain('対象外')
    expect(html).toContain('2026-10-10T07:00:00Z')
    expect(html).toContain('現在値の保証ではありません')
  })

  it('最新の対象外を直前の予想で埋めない', () => {
    const assessment = minkabuOnly(false)
    assessment.minkabu.previous = { ...minkabuOnly(true).minkabu.latest!, fetched_at_utc: '2026-10-09T07:00:00Z', snapshot_date: '2026-10-09' }
    const html = render(assessment)
    expect(html).toContain('対象外')
    expect(html).not.toContain('1,500')
  })

  it('最新の欠測を直前の有効値で埋めない', () => {
    const assessment = minkabuOnly(true)
    assessment.minkabu.previous = { ...assessment.minkabu.latest!, fetched_at_utc: '2026-10-09T07:00:00Z', snapshot_date: '2026-10-09' }
    assessment.minkabu.latest!.target_price_yen = null
    assessment.minkabu.latest!.analyst_eps_yen = null
    const html = render(assessment)
    expect(html).not.toContain('この対象時点以前の保存済み外部評価はありません')
    expect(html).toContain('目標株価 —円')
    expect(html).not.toContain('1,500')
    expect(html).toMatch(/EPS（円）<\/td><td[^>]*>—<\/td><td[^>]*>—<\/td>/)
  })
})
