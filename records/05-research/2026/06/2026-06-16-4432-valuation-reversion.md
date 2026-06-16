---
ticker: '4432'
name: ウイングアーク１ｓｔ
playbook_id: valuation-reversion
playbook_ref:
  ref_path: records/_playbooks/valuation-reversion/2026-05-01T000000+0900.md
  effective_from: '2026-05-01T00:00:00+09:00'
research_decision:
  outcome: approved
  posture: act_now
  reason_code: ai_resilient_software_value_reversion_leftbehind_starter
candidate_ref:
  candidates_ref: records/04-candidates/2026/06/2026-06-12.yaml
  ticker: '4432'
published_at: '2026-06-16T15:30:00+09:00'
recorded_at: '2026-06-16T15:30:00+09:00'
tradable_at: '2026-06-16T15:30:00+09:00'
position_sizing_overlay:
  paper_proxy_position_size_yen: 1150000
  real_order_intent_yen: 234000
  adv_participation_pct: 0.3108
market_cap_oku: 825
sector_33: 情報・通信業
avg_turnover_oku: 3.7
valuation:
  per_forward: null
  per_trailing: 12.56
  pbr: 1.74
  ev_ebitda: null
  p_s: 2.67
  pcfr: null
  ocf_yield: 0.0873
  fcf_yield: null
  net_cash_to_market_cap: 0.093
  cash_to_market_cap: null
  price_to_equity: 1.74
  equity_ratio: 0.64
  primary_metric:
  - per_trailing
  - pbr
macro_context_ref: records/01-macro-context/2026/06/macro-context-2026-06-16-record-high-rotation.yaml
macro_context_fit:
  context_freshness: current
  fit: mixed
  decision_effect: proceed
  required_checks:
  - 情報・通信業は record-high-rotation context で stance=mixed（構造 DX 需要・FX 中立は追い風、BOJ 利上げの discount は高 PER 逆風）。
    本銘柄は予想 PER 約 11 と既に割安で discount 余地が小さい点を確認する。
  - 指数は最高値圏で、割安は銘柄固有の de-rating（半導体ラリーに非参加で取り残された内需 AI/DX ソフト）に偏在する。指数の押し目ではない。
  sizing_caution:
  - event_window_starter_boj_fomc_iran_signing
  - sector_33_information_communication_already_dominant_exposure
thesis_payoff:
  max_entry_price_yen: 2340
  target_price_yen: 2800
  stop_loss_yen: 2120
  expected_upside_pct: 19.66
  expected_downside_pct: 9.40
  risk_reward_ratio: 2.09
  time_horizon_bd: 40
  invalidation_conditions:
  - 永久ライセンス逓減をクラウド/サブスク成長が相殺できず、増収・営業益が反転する。
  - BI（MotionBoard）が Power BI / Microsoft Fabric / Copilot のバンドルに侵食され、データレジデンシー摩擦も縮小して競争力が毀損する。
  - FY2027 営業益 +18% ガイドが 7/14 Q1 以降で下方修正される。
  - macro が悪化（円急反転 / 米テック第2波下落 / 6-19 イラン署名失敗で原油再騰）し stop 2,120 円を割る。
  entry_trigger: user_fill_2340_20260616
entry_preflight:
  evaluated_on: '2026-06-16'
  market_relative_return_pct: -14.06
  sector_or_peer_relative_return_pct: 0.0
  macro_freshness: current
  market_regime:
    regime: risk_on_rally
    benchmark_return_20d: 0.0748
    benchmark_ticker: '1321'
    evaluated_on: '2026-06-16'
  tactical_exposure_after_order:
    sector_33_pct: 41.39
    playbook_pct: 15.41
  near_term_catalyst: false
  action: starter
  reason: >
    2026-06-16 にユーザー指示で 100 株を 2,340 円で約定。market relative は candidate 20d −6.58% に対し
    benchmark proxy 1321 の 20d +7.48% で −14.06pt と大幅劣後（=半導体主導ラリーに乗らず取り残された
    内需 AI/DX ソフトという thesis の定量裏付け。hard trigger に該当するため proceed ではなく starter）。
    sector/peer relative は情報・通信業セクター指数を Tier1 取得できず not_checked（0.0）扱い。macro は current。
    tactical exposure after order は sector 41.39% / playbook（valuation-reversion）15.41% で 50% 上限内だが、
    情報・通信業は deployed notional 比で既に最大の集中（後述 §8）。本日 BOJ・明日 FOMC・6/19 イラン署名の
    連続イベント直前のため 100 株 starter に限定する。
corporate_action_check:
  checked: true
  result: none
  note: candidate 2026-06-12 は split_adjustment_flag=false、price_change_60d −8.89% と異常値域でなく、
    分割・併合・TOB 等の corporate action は確認されない。
tracking:
  mode: post_approval
  plus_15bd: null
  plus_30bd: null
---

# Research: 2026-06-16 4432 ウイングアーク1st valuation-reversion

**成分**: Decision lifecycle の **research / investment memo**（[`/docs/components/research.md`](/docs/components/research.md)）

本メモは 2026-06-16 にユーザー指示で 100 株を 2,340 円で約定した 4432 の execution を documenting する research。screening の canonical fact source は 2026-06-12 candidate row（`valuation-reversion` hit）、定性 thesis の出所は本 PR の AI 傾斜選定と 4432 個別の一次 IR 深掘り（事業/財務・モート・AI 耐性の 3 軸）とする。

## 1. Thesis

- **Swing thesis**: 2026-06-12 candidate で `valuation-reversion` が hit（PER trailing 12.56・PBR 1.74・P/S 2.67、price_change_60d −8.89%・52 週安値 gap 0.0）。株価は 2025 年高値（約 4,050 円）比 約 −42% の de-rating だが、これは業績崩壊でなく**マルチプル圧縮**で、FY2027 会社予想は営業益 +17.9% と加速。半導体主導の最高値ラリーに非参加で取り残された内需 AI/DX ソフトの mean-reversion を狙う。
- **Long-hold fallback**: 高い。ネットキャッシュ約 76.5 億（時価総額の約 9%）・自己資本比率 64%・保守継続率 93.4%・営業利益率 29%・配当利回り 4.58%。クラウド移行が**希薄化でなく増益的**（リカーリング 61%→66%、EBITDA マージン Q4 29%→38%）で、含み損ロック時も配当と財務健全性が支える。
- **Capital lock / shareholder return**: 配当利回り 4.58%・総還元方針 約 50%・6 年連続増配。自己株買いはほぼ未実施＝ROE 引き上げの未活用レバー（資本配分の積極化が上振れ材料）。
- **AI long-term impact**: 中〜高（耐性側）。ユーザー懸念「SaaS は価値が下がるかもしれない」を一次調査で検証した。2026 年のセクター見解（Bain / Stratechery / Oliver Wyman / Starburst）は、AI が毀損するのは**薄い UI 層・単純ワークフロー・seat 課金・コモディティ抽出**で、**独自データ・コンプラ/監査ロックイン・System of Record・決定論的な構造化出力**はむしろ強化されるとする。本銘柄は重心（売上の約 63%＝帳票 SVF/invoiceAgent）が後者に深く食い込む：国内シェア約 70%・42,000 社超、電帳法 JIIMA 認証、インボイス対応、デジタル庁認定 Peppol サービスプロバイダー（電子インボイス規格移行の関所）。AI 普及はむしろ信頼できる構造化出力の需要を増やす（新製品 Trustee = AI 生成物の真正性）。残り 37%＝BI（MotionBoard）はダッシュボード可視化として脆弱面だが、SVF/Dr.Sum との国産スタック一体＋日本データレジデンシー摩擦（Microsoft Copilot は日本容量でも米国処理・ソブリン未対応）が当面の防壁。AI は単独の採用・sizing 根拠にはせず、valuation・CF・財務と合わせて判断する。

## 2. Macro context

- **macro_context_ref**: `records/01-macro-context/2026/06/macro-context-2026-06-16-record-high-rotation.yaml`
- **context_freshness**: current（as_of 2026-06-16 / valid_until 2026-06-23、約定 2026-06-16 は window 内）
- **fit**: mixed（情報・通信業の sector_tilt stance = mixed：構造 DX 需要・FX 中立は追い風、BOJ 利上げの discount は高 PER 逆風）
- **decision_effect**: proceed（予想 PER 約 11 と既に割安で discount 余地が小さく、半導体ラリーに乗らなかった取り残されバリュー側）
- **required_checks**: 指数は最高値圏で割安は銘柄固有の de-rating に偏在する点、BOJ 利上げの高 PER 逆風を割安度が相殺するか。
- **sizing_caution**: 連続イベント週の starter、情報・通信業は既に最大の集中（§8）。

## 3. Valuation snapshot

| 指標 | 値 | source |
| --- | ---: | --- |
| PER (trailing) | 12.56 | 2026-06-12 candidate row |
| 予想 PER | 約 11 | 会社予想 FY2027 当期益 74.2 億ベース（IR / kabuyoho） |
| PBR | 1.74 | 2026-06-12 candidate row |
| P/S | 2.67 | 2026-06-12 candidate row |
| OCF yield | 8.73% | 2026-06-12 candidate row |
| 自己資本比率 | 64.0% | candidate row / IR |
| 配当利回り (予想) | 4.58% | IR（FY2027 予想 108 円） |
| price_change_20d / 60d | −6.58% / −8.89% | candidate row |
| gap_from_52w_low | 0.0（52 週安値） | candidate row |

予想 PER 約 11 が織り込む長期成長は簡易 Gordon（配当性向 55%、r 8.5%）で約 3〜4%。会社の営業益 +17.9%/当期 +14.2%/売上 +10.8% ガイドと不整合で、ミスプライスの蓋然性。

## 4. 一時的割安の原因仮説

- 2026 年の SaaS セクター全体の AI 起因 de-rating（ソフトの予想 PER 84→22.7 倍、史上初の S&P500 ディスカウント、IGV 約 $2 兆消失）の一部。Oliver Wyman は 2026 初のエージェント AI 実証が金利と独立した売りを誘発と整理。
- 個別では、6/8 暴落後の最高値ラリーが半導体・電機・輸出主導で、内需ソフトが非参加（4432 の 20d は benchmark 比 −14pt）。永久ライセンス −5% の構造ドラッグで増収率が低二桁に見える点も嫌気。
- いずれも業績崩壊でなくマルチプル圧縮（FY2027 営業益 +18% ガイド）＝ valuation-reversion の前提に合致。

## 5. 反対仮説（弱気・SaaS 価値低下シナリオ）

- **BI コモディティ化**: MotionBoard 単体は Power BI（seat 2,098 円〜、M365 バンドル）/ Microsoft Fabric+Copilot に劣後しうる。「ダッシュボードは最初に消える」（Starburst）。防壁は SVF/Dr.Sum スタック一体＋日本データレジデンシー摩擦で、これが縮小すれば BI 側から漸進毀損。
- **帳票の単純抽出部分**: 請求書受領/OCR 抽出はコモディティ化側（Bain=Tipalti）。発行・統制・保存の System of Record を握れるかが分水嶺。
- **ミックス目標遅延**: 旧中計のリカーリング 75%/クラウド 40% は FY2026 で 66%/約 23% と乖離、長期化。
- **ROE 希薄化**: 自己資本比率 64% 上昇・余剰現金・自社株買い僅少で内部留保が ROE を下押し。
- これらが重なれば「低二桁成長」が「無成長」へ劣化する。ただし規制係留（Peppol 関所・電帳法・JIIMA）と 70% シェアで、毀損は崖でなく緩やかな侵食と判断。

## 6. Catalyst

- **7/14 Q1 決算**（近接 catalyst）。クラウド/サブスク +35% 成長の継続・FY2027 通期ガイド据置の確認。
- クラウドアップセル（Dr.Sum Cloud 138 社のみ＝浸透余地大）、公共 DX（子会社 NEX、自治体 400 団体超、電子請求市場 +82%）、新製品 Trustee、値上げ。

## 7. Price reaction

2025 年高値（約 4,050 円、PER 約 22）→ 2026-06-12 終値 2,356 円 → 2026-06-16 約定 2,340 円。高値比 約 −42% の de-rating。candidate の 60d −8.89% は半導体ラリー局面で内需ソフトが置いてけぼりになった分。52 週安値圏（gap 0.0）。

## 8. Positioning / liquidity（集中度の honest flag）

- avg_turnover_oku 3.7（流動性母集団内）。adv participation は paper proxy 1,150,000 円ベースで 0.3108%（§13）。
- **重要（正直な集中度開示）**: 約定前の deployed notional 1,057,400 円のうち**情報・通信業が 56.1%（593,700 円：9470/9682/9692）で ledger が「追加前に exposure review 必須」と警告**している。4432（情報・通信業）の追加で、情報・通信業の deployed share は約 64% に上昇する。
- 一方、**playbook は分散方向**：4432 は valuation-reversion（既存は 9534 のみ 74,200 円＝7%）で、過集中している sales-discount-growth（56.1%、情報・通信業の 3 銘柄）には足さない。
- policy cap（trade validator, 実資金 500 万円基準）では sector 45%＝225 万円に対し情報・通信業 82.77 万円、ticker 8%＝40 万円に対し 23.4 万円、playbook 35%＝175 万円に対し 30.82 万円、tactical 200 万円に対し合計 129.14 万円で**全て上限内**。だが deployed share の集中は実在するため、**情報・通信業への追加買いは当面避け、4432 は starter（100 株）に留める**。

## 9. Shareholder return

配当利回り 4.58%（FY2027 予想 108 円・6 年連続増配）、総還元方針 約 50%、配当性向 55.6%。自己株買いはほぼ未実施で、ROE 引き上げの未活用レバー。資産ロック中の収益は配当が主な支え。

## 10. Entry

ユーザー指示・確認により、2026-06-16 に 100 株を 2,340 円で約定。実 notional 234,000 円（100 株 × 2,340 円）。実資金 5,000,000 円比 4.68%、tactical budget 2,000,000 円比 11.70%。連続イベント週のため 100 株 starter に限定。

### Entry preflight

| Check | Value | Source / note |
| --- | --- | --- |
| Price window | basis: candidate asof 2026-06-12 / fill: 2026-06-16 | candidate row + user fill |
| Market baseline | benchmark proxy 1321 20d +7.48% | SQLite daily bars |
| Relative return | vs market: −14.06pt / vs sector-peer: 0.0pt (not_checked) | candidate 20d −6.58% − 1321 20d +7.48% |
| Macro freshness | current | window 内（[6-16, 6-23]） |
| Exposure after order | sector 41.39% / playbook 15.41%（分母 tactical 200 万円） | deployed share では情報・通信業 約 64%（§8） |
| Action | starter | market relative −14pt の hard trigger ＋連続イベント週＋sector 集中のため 100 株 starter |

- **約定価格 / guard**: 2,340 円
- **quantity / board lot**: 100 株 / 100

## 11. Exit

初期 target は 2,800 円（+19.66%、予想 PER 約 13 への部分リレート）、stop は 2,120 円（−9.40%）。40 営業日 time stop は 2026-08-12 目安。7/14 Q1 で FY2027 ガイド下方修正、または BI 競争力毀損・クラウド移行失速が確認された場合は価格にかかわらず review する。外れて売却を逃した場合は配当 4.58%＋ネットキャッシュで長期保有へ切り替える。

## 12. Invalidation

- 永久ライセンス逓減をクラウド/サブスク成長が相殺できず、増収・営業益が反転する。
- BI（MotionBoard）が Microsoft バンドルに侵食され、データレジデンシー摩擦も縮小して競争力が毀損する。
- FY2027 営業益 +18% ガイドが 7/14 Q1 以降で下方修正される。
- macro 悪化（円急反転 / 米テック第2波下落 / 6-19 イラン署名失敗）で stop 2,120 円を割る。

## 13. Position size

- decision: approved（ユーザー 2026-06-16 実弾約定済み、100 株 @ 2,340 円）。
- paper proxy: 1,150,000 円。
- real order intent: 234,000 円（100 株 × 2,340 円）。
- ADV participation: 1,150,000 / 3.7 億円 × 100 = 0.3108%。
- 実資金 concentration: 234,000 / 5,000,000 = 4.68%。tactical concentration: 234,000 / 2,000,000 = 11.70%。
- binding cap: paper→real 21% scaled cap（241,500 円）内。追加買いは情報・通信業の集中（§8）と 7/14 Q1 の確認後に再判断する。

参照: [`/docs/components/research.md`](/docs/components/research.md), [`#242`](https://github.com/koumatsumoto/baibai-loop/pull/242), [`#243`](https://github.com/koumatsumoto/baibai-loop/issues/243)
