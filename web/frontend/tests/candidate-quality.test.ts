import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'

import type { StockAnalysisRowView } from '../src/api/types'
import { CandidateSection } from '../src/pages/stocks/CandidateSection'
import { stockAnalysisRow, stockRun, candidateMethod } from './helpers/stock-analysis'

type Reinvestment = Pick<StockAnalysisRowView, 'p_s_sector_gap' | 'operating_return_on_capital_proxy' | 'sales_yoy' | 'operating_margin' | 'fcf_yield'>

function renderQuality(
  ev: 'exact' | 'approximated' | 'unavailable' | null | undefined,
  fcf: 'exact' | 'approximated' | 'unavailable' | null | undefined,
  carry: Partial<StockAnalysisRowView> = {},
  reinvestment: Reinvestment | null = null,
  averagePer: number | null = null,
  earningsStatus: string | null = null,
  staleFinFlag: boolean | null = null,
): string {
  const row = stockAnalysisRow({
    normalized_per_3fy: averagePer, stale_fin_flag: staleFinFlag,
    ttm_quality_ev_ebitda: ev ?? null, ttm_quality_fcf: fcf ?? null,
    next_earnings_status: earningsStatus as StockAnalysisRowView['next_earnings_status'],
    ...reinvestment, ...carry,
  })
  return renderToStaticMarkup(createElement(MemoryRouter, null,
    createElement(CandidateSection, { rows: [row], run: stockRun, method: candidateMethod }),
  ))
}

describe('candidate TTM quality', () => {
  it('describes average EPS and the limited CF role without changing frozen numbers', () => {
    const html = renderQuality('exact', 'exact', {}, null, 11.25)
    expect(html).toContain('3FY平均EPSベースPER')
    expect(html).toMatch(/3FY平均EPSベースPER<\/dt><dd[^>]*>11.25<\/dd>/)
    expect(html).toContain('3FY平均EPSは一時損益・循環を調整した代表利益ではありません。')
    expect(html).toContain('利益倍率の探索でCFは同順位時の補助です。')
    expect(html).not.toContain('平常PER')
    expect(html).not.toContain('平常収益力')
  })

  it.each([
    ['exact', 'exact', false, false],
    ['approximated', 'unavailable', true, true],
    [null, null, false, false],
    [undefined, undefined, false, false],
  ] as const)('shows only nonexact values: %s / %s', (ev, fcf, showEv, showFcf) => {
    const html = renderQuality(ev, fcf)
    expect(html.includes('EV/EBITDA TTM')).toBe(showEv)
    expect(html.includes('FCF TTM')).toBe(showFcf)
    if (showEv) expect(html).toContain('approximated')
    if (showFcf) expect(html).toContain('unavailable')
  })
})

describe('partial Reinvestment observations', () => {
  it.each([
    [-0.01, '-1%'],
    [0, '0%'],
    [null, '—'],
  ] as const)('distinguishes known growth %s from missing growth', (salesYoY, expected) => {
    const html = renderQuality(null, null, {}, {
      p_s_sector_gap: -0.4, operating_return_on_capital_proxy: 0.1,
      sales_yoy: salesYoY, operating_margin: null, fcf_yield: 0.08,
    })
    expect(html).toMatch(new RegExp(`sales YoY</dt><dd[^>]*>${expected}</dd>`))
    expect(html).toMatch(/operating margin<\/dt><dd[^>]*>—<\/dd>/)
    expect(html).toMatch(/P\/S sector gap<\/dt><dd[^>]*>-0.4<\/dd>/)
    expect(html).not.toContain('not applicable')
  })
})

describe('machine carry inputs', () => {
  it('distinguishes missing inputs from observed zero with the same carry', () => {
    const missing = renderQuality(null, null, {
      er_carry_annual: 0, dividend_basis: 'unavailable', carry_missing_assumption: 'synthetic assumption',
    })
    const zero = renderQuality(null, null, {
      er_carry_annual: 0, dividend_yield: 0, net_share_change_yoy: 0,
      dividend_basis: 'actual_reported', carry_missing_assumption: 'synthetic assumption',
    })
    expect(missing).toContain('未観測・未記録')
    expect(missing).toContain('unavailable')
    expect(zero).not.toContain('未観測・未記録')
    expect(zero).toContain('actual_reported')
    expect(zero).toContain('0%')
    expect(missing).toContain('欠測の成分は計算上0寄与。無配・株数不変の確認ではありません。')
    expect(zero).toContain('欠測の成分は計算上0寄与。無配・株数不変の確認ではありません。')
  })

  it('shows that an old frozen snapshot has no carry metadata', () => {
    expect(renderQuality(null, null)).toContain('この保存snapshotには仮定の記録がありません。')
  })
})

describe('earnings calendar annotation', () => {
  it.each(['scheduled_date_reached'])(
    'renders %s as a passed scheduled date without claiming disclosure', (status) => {
      const html = renderQuality(null, null, {}, null, null, status, true)
      expect(html).toContain('予定日到来・経過（発表未確認）')
      expect(html).toContain('予定日到来・財務未照合')
      expect(html).not.toContain('>announced<')
    },
  )
})
