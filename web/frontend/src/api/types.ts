// Generated from web/backend/src/baibai_web/readmodel/models.py.
// Run `uv run python -m baibai_web.contracts_export` after changing those models;
// `tools/quality/drift/check_readmodel_contract.py` refuses a stale copy.

export interface DashboardView {
  generated_at: string
  ledger_exists: boolean
  ledger_error: string | null
  ledger_as_of: string | null
  /**
   * 市場評価の読取失敗。台帳事実は保持し、株価等の欠測だけの場合はnull。
   */
  valuation_error: string | null
  valuation_as_of: string | null
  total_capital_yen: number | null
  /**
   * 台帳上の未予約現金。売却代金は約定時にgrossで計上し、受渡済み資金や証券会社の発注可能額は表さない。
   */
  available_cash_yen: number | null
  reserved_cash_yen: number | null
  holdings_market_value_yen: number | null
  deployed_cost_yen: number | null
  realized_gross_pnl_yen: number | null
  cash_pct: number | null
  reserved_pct: number | null
  deployed_pct: number | null
  holdings: HoldingView[]
  reservations: ReservationView[]
  warnings: WarningView[]
}

export interface HoldingView {
  ticker: string
  company_name: string | null
  sector: string
  quantity: number
  deployed_cost_yen: number
  market_price_yen: string | null
  market_price_as_of: string | null
  market_value_yen: number | null
  unrealized_pnl_yen: number | null
  unrealized_pnl_pct: number | null
}

export interface OutcomesView {
  outcomes: PortfolioOutcomeView[]
}

export interface PortfolioOutcomeView {
  outcome_id: string
  horizon: string
  period_start_date: string
  period_end_date: string
  status: string
  reason: string | null
  /**
   * 記録済みの税・費用を反映した期間return。未報告の税・費用をゼロ確定とみなさない。
   */
  portfolio_twr_pct: number | null
  benchmark_cumulative_return_pct: number | null
}

export interface ReservationView {
  reservation_id: string
  side: 'buy' | 'sell'
  ticker: string
  sector: string
  remaining_quantity: number
  price_guard_yen: string
  reserved_yen: number
  expires_at: string | null
  expiry_label: string | null
  route_label: string | null
}

export interface TaskView {
  task_id: string
  title: string
  kind: string
  status: string
  ticker: string | null
  due_date: string
  event_label: string | null
  event_date: string | null
}

/**
 * Task/event workflow projection, separate from portfolio presentation.
 */
export interface TasksView {
  generated_at: string
  tasks_exist: boolean
  open_tasks: TaskView[]
  next_task: TaskView | null
  upcoming_events: UpcomingEventView[]
  /**
   * 保有決算予定の確認可否。保有なし/台帳不明はnull。
   */
  earnings_calendar_available: boolean | null
  /**
   * 決算カレンダーの読取失敗。欠測だけの場合はnull。
   */
  earnings_calendar_error: string | null
  ledger_error: string | null
}

export interface UpcomingEventView {
  event_date: string
  kind: 'earnings' | 'reservation_expiry'
  ticker: string | null
  label: string
}

export interface WarningView {
  code: string
  scope: string
  key: string
  actual_pct: number
  warning_pct: number
  overridden: boolean
}
