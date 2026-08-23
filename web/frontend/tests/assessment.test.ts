import { describe, expect, it } from 'vitest'

import type { AssessmentCaseView, AssessmentPurchaseView } from '../src/api/types'
import { ASSESSMENT_RESULT, CASE_DISPOSITION, headroomToMaxPct, limitVsClosePct, orderCases, purchaseAlerts } from '../src/lib/assessment'

function assessmentCase(ticker: string, disposition: string): AssessmentCaseView {
  return {
    ticker,
    name: null,
    disposition,
    disposition_reason: '',
    thesis_id: `thesis-${ticker}`,
    review_id: null,
    five_year_base_cagr_pct: null,
    required_return_pct: null,
    fair_value_yen: null,
    fv_gap_pct: null,
    base_terminal_multiple: null,
    break_even_terminal_multiple: null,
    terminal_multiple_buffer: null,
    break_even_earnings_growth_pct: null,
    earnings_growth_buffer_pp: null,
    observed_trailing_multiple: null,
    business_model: '',
    value_capture: '',
    growth_quality: '',
    financial_resilience: '',
    strongest_countercase: '',
    catalyst: '',
    unknowns: [],
    source_caveats: [],
  }
}

function purchase(overrides: Partial<AssessmentPurchaseView> = {}): AssessmentPurchaseView {
  return {
    proposal_id: 'proposal-20260728-2331-1',
    ticker: '2331',
    limit_price_yen: 1050,
    quantity: 400,
    notional_yen: 420_000,
    max_acceptable_price_yen: 1102.5,
    close_yen: 1000,
    price_as_of: '2026-07-28',
    expires_at: '2026-07-31T15:00:00+09:00',
    warnings: [],
    current_status: 'pending',
    superseded: false,
    ...overrides,
  }
}

describe('orderCases', () => {
  it('leads with the selected case and keeps the rest in published order', () => {
    const ordered = orderCases([assessmentCase('1111', 'reject'), assessmentCase('2222', 'defer'), assessmentCase('3333', 'selected')])
    expect(ordered.map((item) => item.ticker)).toEqual(['3333', '1111', '2222'])
  })

  it('keeps published order when no case was selected', () => {
    // no_actionable_bargain and defer cycles have no selected case, and their reading
    // order is the order the assessment argues them in.
    const ordered = orderCases([assessmentCase('1111', 'reject'), assessmentCase('2222', 'defer')])
    expect(ordered.map((item) => item.ticker)).toEqual(['1111', '2222'])
  })

  it('does not mutate the input', () => {
    const cases = [assessmentCase('1111', 'reject'), assessmentCase('2222', 'selected')]
    orderCases(cases)
    expect(cases.map((item) => item.ticker)).toEqual(['1111', '2222'])
  })
})

describe('purchaseAlerts', () => {
  it('stays silent on a live pending plan', () => {
    expect(purchaseAlerts(purchase(), new Date('2026-07-29T09:00:00+09:00'))).toEqual([])
  })

  it('warns once the order expiry has passed', () => {
    const alerts = purchaseAlerts(purchase(), new Date('2026-08-01T09:00:00+09:00'))
    expect(alerts).toHaveLength(1)
    expect(alerts[0].severity).toBe('warning')
    expect(alerts[0].message).toContain('発注期限')
  })

  it('warns when the proposal no longer exists', () => {
    const alerts = purchaseAlerts(
      purchase({ current_status: null, superseded: true }),
      new Date('2026-07-29T09:00:00+09:00'),
    )
    expect(alerts.map((alert) => alert.severity)).toEqual(['warning'])
    expect(alerts[0].message).toContain('application DB')
  })

  it('reports a decided proposal as information rather than a warning', () => {
    const alerts = purchaseAlerts(
      purchase({ current_status: 'approved' }),
      new Date('2026-07-29T09:00:00+09:00'),
    )
    expect(alerts).toEqual([{ severity: 'info', message: 'publish 後に approved へ動いています。' }])
  })

  it('reports expiry and supersession together', () => {
    const alerts = purchaseAlerts(
      purchase({ current_status: null, superseded: true }),
      new Date('2026-08-01T09:00:00+09:00'),
    )
    expect(alerts).toHaveLength(2)
  })

  it('ignores an unparseable expiry instead of reading it as expired', () => {
    expect(purchaseAlerts(purchase({ expires_at: 'not-a-timestamp' }), new Date('2026-08-01T09:00:00+09:00'))).toEqual([])
  })
})

describe('price ratios', () => {
  it('reads the limit against the close it was struck from', () => {
    expect(limitVsClosePct(purchase())).toBeCloseTo(5, 10)
  })

  it('reads the room left before the price stops being worth paying', () => {
    expect(headroomToMaxPct(purchase())).toBeCloseTo(5, 10)
  })

  it('returns null rather than dividing by a non-positive price', () => {
    expect(limitVsClosePct(purchase({ close_yen: 0 }))).toBeNull()
    expect(headroomToMaxPct(purchase({ limit_price_yen: 0 }))).toBeNull()
  })
})

describe('vocabulary', () => {
  it('labels every result the engine can publish', () => {
    for (const result of ['proposal', 'no_actionable_bargain', 'defer']) {
      expect(ASSESSMENT_RESULT[result]).toBeDefined()
    }
  })

  it('labels every case disposition the engine can publish', () => {
    for (const disposition of ['selected', 'reject', 'defer']) {
      expect(CASE_DISPOSITION[disposition]).toBeDefined()
    }
  })
})
