// 合成データのみ。新規と保有を別の保存scopeとして扱い、実データを複写しない。
import type {
  LatestProposalView,
  SavedAssessmentView,
  SavedBuyItemView,
  SavedDeferItemView,
  SavedHoldItemView,
  SavedLimitTermsView,
  SavedNoActionItemView,
  SavedProposalView,
  SavedSellItemView,
} from '../../src/api/types'

export function assessmentFixture(label: string): SavedAssessmentView {
  return {
    assessment_id: `assessment-${label}`,
    core_sha256: label === 'original-a' ? 'a'.repeat(64) : 'b'.repeat(64),
    review_id: `assessment-review-${label}`,
    as_of: '2026-10-26',
    stance: 'supported',
    investment_case_explanation: `${label}の投資理由。利益改善と現金還元を確認する。\n需要減退の場合は前提を見直す。`,
    investment_case_status: 'intact',
    investment_case_status_reason: `${label}の状態理由。主要顧客の継続を保存資料で確認。`,
    invalidation_conditions: [`${label}の反証条件：主要顧客の離脱`],
    review: {
      reviewed_at: '2026-10-26T08:15:30+09:00',
      primary_source_check: 'partially_verified',
      review_scope: `${label}の検証範囲：公開資料の売上・利益。未開示の契約更新率は未確認。`,
      strongest_countercase: `${label}の反対論：利益率改善が一過性の可能性。`,
      uncertainty_acceptance_reason: `${label}の引受理由：未確認事項の影響を限定した条件付き判断。`,
    },
  }
}

export const termsFixture: SavedLimitTermsView = {
  limit_price_yen: '1200.0000',
  quantity: 100,
  notional_yen: '120000.0000',
  target_session: '2026-10-28',
  valuation_as_of: '2026-10-27',
  maximum_entry_price_yen: '1250.0000000000000001',
  conditions: ['未約定注文・cashの変化がないこと。', '新情報が出た場合は再確認する。'],
  warnings: ['合成の具体条件warning。約定を保証しない。'],
}

export const buyFixture: SavedBuyItemView = {
  ticker: '1111',
  action: 'buy',
  rationale: '合成の買付理由。回収経路と損失経路を比較して指値を採った。',
  review_conditions: ['未約定・部分約定後は残注文を確認する。'],
  assessment: assessmentFixture('buy'),
  saved_quote: { price_yen: '1300.0000000000000001', price_as_of: '2026-10-26', price_basis: 'last_close_unadjusted' },
  liquidity: { analysis_as_of: '2026-10-26', adv_yen: '9876543.0000000001', unavailable_reason: null },
  unfinished: null,
  timing: 'wait_limit',
  terms: termsFixture,
  proposed_limit_yen: null,
  terms_unavailable_reasons: [],
}

export const partialBuyFixture: SavedBuyItemView = {
  ...buyFixture,
  ticker: '1112',
  rationale: '作者の購入上限は保存済みだが、分類不足で具体条件を確定していない。',
  timing: 'place_limit',
  terms: null,
  proposed_limit_yen: '1400.1000000000000001',
  terms_unavailable_reasons: [{ code: 'security_classification_unavailable', message: '保存された銘柄分類を確認できない。' }],
}

const noActionFixture: SavedNoActionItemView = {
  ticker: '2222',
  action: 'no_action',
  rationale: '完成した経済比較の結果、現金維持を選択する。',
  review_conditions: ['企業価値の見積りが変われば比較を再開する。'],
  assessment: assessmentFixture('no-action'),
  saved_quote: null,
  liquidity: null,
  unfinished: null,
}

export const reviewedDeferFixture: SavedDeferItemView = {
  ticker: '3333',
  action: 'defer',
  rationale: '確認範囲が限られるため、今回の判断は保留する。',
  review_conditions: ['契約更新率の一次資料を確認する。'],
  assessment: {
    ...assessmentFixture('defer'),
    stance: 'uncertain',
    investment_case_status: 'uncertain',
    investment_case_status_reason: '主要契約の継続を判断しきれない。',
  },
  saved_quote: null,
  liquidity: { analysis_as_of: null, adv_yen: null, unavailable_reason: '売買代金の保存モデルが未取得。' },
  unfinished: null,
  blockers: [{ code: 'investment_case_uncertain', message: '投資仮説に未解決の不確実性が残る。' }],
  resume_conditions: ['更新率の一次資料が公表されたら再評価する。'],
}

export const unassessedDeferFixture: SavedDeferItemView = {
  ...reviewedDeferFixture,
  ticker: '3334',
  rationale: '調査が未完了のため、評価を作らず保留した。',
  assessment: null,
  unfinished: { reason: '合成の未完了理由：必要な一次資料が未入手。', references: ['synthetic:research-missing-document'] },
  blockers: [{ code: 'research_unfinished', message: '必要な一次資料が未入手。' }],
  resume_conditions: ['必要な一次資料を入手したら調査を再開する。'],
}

export const holdFixture: SavedHoldItemView = {
  ticker: '4444',
  action: 'hold',
  rationale: '今回の評価で当初の仮説維持を確認した。',
  review_conditions: ['主要顧客の離脱時に当初理由を再点検する。'],
  assessment: assessmentFixture('current-b'),
  saved_quote: null,
  liquidity: null,
  unfinished: null,
  original_assessment: { ...assessmentFixture('original-a'), as_of: '2026-07-01' },
  original_investment_reason: null,
  original_reason_unavailable_reason: null,
  case_reassessment_reason: '今回の再評価では当初契約の継続を確認した。',
  remaining_reward: null,
  holding_basis: 'investment_case_intact',
  return_calculation_unavailable_reason: '当日quote不足のため、現在の収益率が十分とは主張できない。',
}

export const sellFixture: SavedSellItemView = {
  ticker: '5555',
  action: 'sell',
  rationale: '主要契約の終了によって当初の利益経路が成立しなくなった。',
  review_conditions: ['数量basisと注文状況を再確認する。'],
  assessment: { ...assessmentFixture('sell'), stance: 'unsupported', investment_case_status: 'broken' },
  saved_quote: null,
  liquidity: null,
  unfinished: null,
  original_assessment: null,
  original_investment_reason: '当初保存した理由本文：長期契約の収益を取得理由とした。',
  original_reason_unavailable_reason: null,
  case_reassessment_reason: '今回確認した契約終了は、当初理由の重大な不成立に当たる。',
  remaining_reward: null,
  reason_code: 'economic_invalidation',
  terms: null,
  terms_unavailable_reasons: [{ code: 'quantity_basis_unconfirmed', message: '保存数量のbasisが未確認で、具体売却条件は未確定。' }],
}

const proposalBase: Omit<SavedProposalView, 'request_scope' | 'items'> = {
  proposal_id: 'investment-proposal-00000000-0000-4000-8000-000000000001',
  as_of: '2026-10-27',
  prepared_at: '2026-10-27T07:00:01+09:00',
  published_at: '2026-10-27T09:00:02+09:00',
  headline: '合成の保存提案。対象を比較して、採る行動と見送る理由を記録する。',
  comparison: '比較の全文。\n現金を維持する案と、条件付きで資本を投下する案を比べた。',
  forgone: '採らなかった案の全文。未約定時の機会だけでなく回避できる損失も考慮した。',
  warnings: ['合成の全体warning。未確認部分を現在値で補完していない。'],
  portfolio_exposure: {
    total_capital_yen: '1000000.0000000001',
    ticker: { key: '1111', prospective_pct: '12.0000000000000001', warning_pct: '20' },
    sector: { key: '合成の業種', prospective_pct: '30.0000000000000001', warning_pct: '35' },
    common_factors: [{ key: '合成の共通要因', prospective_pct: '40.0000000000000001', warning_pct: '40' }],
    common_factor_unclassified_tickers: ['9999'],
  },
}

export const newProposalFixture: SavedProposalView = {
  ...proposalBase,
  request_scope: 'discover_new',
  items: [buyFixture, noActionFixture, reviewedDeferFixture, unassessedDeferFixture],
}

export const holdingProposalFixture: SavedProposalView = {
  ...proposalBase,
  proposal_id: 'investment-proposal-00000000-0000-4000-8000-000000000002',
  request_scope: 'review_positions',
  portfolio_exposure: { total_capital_yen: null, ticker: null, sector: null, common_factors: [], common_factor_unclassified_tickers: ['4444'] },
  items: [holdFixture, sellFixture, {
    ...holdFixture,
    ticker: '4445',
    original_assessment: null,
    original_reason_unavailable_reason: '当初の取得判断は保存されていない。',
    holding_basis: 'sufficient_remaining_reward',
    remaining_reward: { status: 'sufficient', reason: '保存した当日起点の算術では十分な残存見返り。' },
    return_calculation_unavailable_reason: null,
  }],
}

export function proposalViewFixture(proposal: SavedProposalView = newProposalFixture): LatestProposalView {
  return { generated_at: '2026-10-29T02:30:03Z', status: 'ready', error: null, proposal }
}
