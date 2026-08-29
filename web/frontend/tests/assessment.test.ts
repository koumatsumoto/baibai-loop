import { describe, expect, it } from 'vitest'

import type { AssessmentCaseView } from '../src/api/types'
import { ASSESSMENT_RESULT, CASE_DISPOSITION, orderCases } from '../src/lib/assessment'

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

describe('vocabulary', () => {
  it('labels every result the engine can publish', () => {
    for (const result of ['buy', 'no_actionable_bargain', 'defer']) {
      expect(ASSESSMENT_RESULT[result]).toBeDefined()
    }
  })

  it('labels every case disposition the engine can publish', () => {
    for (const disposition of ['selected', 'reject', 'defer']) {
      expect(CASE_DISPOSITION[disposition]).toBeDefined()
    }
  })
})
