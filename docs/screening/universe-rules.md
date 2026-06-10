# screening/universe-rules.md

Baibai-Loop スクリーニングの対象範囲(scope)と、規模・流動性パラメータの適用位置。`records/04-candidates/` の入力となる銘柄 pool を定義する。

## 1. 設計原則: scope は広く、絞り込みは分析層で

screen の評価対象(scope)は**全上場普通株**とし、規模・流動性・上場期間・規制 flag は除外条件ではなく **candidates に記録される事実**として扱う。research 候補の絞り込み(時価総額・売買代金・上場期間・JPX 規制)は分析層のパラメータ(`selection.liquidity`)として selection 時に適用する。

- データを狭めない: どの銘柄も screening 事実(lane 判定・valuation・流動性)を持つため、ticker-profile や lane-cohorts が universe 外の銘柄も同じ事実で扱える
- 絞り込みは可視・可変: 何件がどの条件で落ちたかは selection の `diagnostics.liquidity_excluded_count` に出る。閾値は config と `--profile-config` で変更でき、変更は replay / ablation で計測してから採用する

## 2. Scope(構造的な対象範囲)

| 条件 | 値 | 理由 |
| --- | --- | --- |
| 銘柄種別 | 普通株のみ(ETF / REIT / 優先株を除外) | 事業会社の valuation 判定が対象 |
| 上場市場 | プライム / スタンダード / グロース(TOKYO PRO Market 対象外) | 一般投資家が取引可能な市場 |
| bar 履歴 | 直近 20 営業日以上 | 短期リターン・売買代金 fact の算出に必要な最小データ |

scope 外は `universe_exclusion_lines` に件数を記録する(`non_common_stock` / `market_out_of_scope` / `insufficient_bar_history`)。

## 3. 銘柄ごとに記録する事実

| field | 算出 | 備考 |
| --- | --- | --- |
| `market_cap_oku` | 直近営業日終値 × 発行済株式数 | 株式数欠損時は `null` |
| `avg_turnover_oku` | 直近 20 営業日売買代金の単純平均 | 欠損日があれば `null` |
| `listing_span_days` | daily bars 履歴の最古日からの日数 | J-Quants v2 master に listing_date が無いための proxy。cache window は asof-1200 日のため、上場 3.3 年超は 1200 日扱いになるが閾値判定には影響しない |
| `jpx_flags` | JPX 公開規制情報(特別注意 / 整理 / 取引停止 / 上場廃止警告) | 事実として記録。除外判断は selection 側 |

## 4. 分析層の絞り込みパラメータ(`selection.liquidity`)

research 推奨を作るときに適用する。正本は `records/_config/screening-rules/` の `selection.liquidity`。

| パラメータ | 既定値 | 意味 |
| --- | --- | --- |
| `min_market_cap_oku` | 100 | 時価総額下限 |
| `min_avg_turnover_oku` | 1.0 | 20 営業日平均売買代金下限(億円) |
| `min_listing_span_days` | 182 | IPO 直後の値動き特殊性を避ける |
| `exclude_jpx_flagged` | true | `universe.required_jpx_flags` に該当する銘柄を推奨から除外 |

- 判定は candidates に記録された丸め後の事実(`market_cap_oku` は整数、`avg_turnover_oku` は小数 1 桁)に対して行う(記録された事実 = 判定対象を一致させるため)
- 事実が `null` の候補(4 fact のいずれか欠損)は filter を通過させ、`diagnostics.liquidity_fact_missing_count` で可視化する(欠損を黙って除外しない)
- `--profile-config` による liquidity の上書きは selection filter にのみ効く。中央値の比較母集団(§5)は base config で固定
- 日々公表信用指定は除外対象に含めない。research で positioning / liquidity risk として記録する
- 上場 3 年未満は scope に含め、過去 3 年自己レンジが不足する指標は上場来レンジで代替する

## 5. Valuation 比較の母集団

sector / 市場中央値と sector relative strength の比較母集団は、**`selection.liquidity` を満たす流動性母集団**で固定する(算出仕様は [`valuation-metrics.md`](./valuation-metrics.md))。判定は単一の述語(`SelectionLiquidityRules.matches`)を selection filter と共有し、母集団側は事実が揃っている銘柄だけを含める(`require_facts`)。母集団サイズと事実欠損件数は run の `provider_status_lines` に記録される。scope の全銘柄がこの投資可能な比較対象に対して判定されるため、小型・低流動性銘柄の混入で判定基準が歪まない。

## 6. 時価総額別の Position sizing 上限

詳細は [`principles.md`](./principles.md) 節 7 を参照。核心:

| 条件 | Position 上限 | 備考 |
| --- | --- | --- |
| single primary evidence path | 1% | 標準 |
| 複数 independent evidence paths | 2% | 複数の独立した割安根拠が重なる場合 |

## 7. Front matter 書式ルール(universe 関連)

### 7.1 日時

- ISO 8601 完全形(秒まで)、**quote 必須**
- 例: `"2026-04-24T09:00:00+09:00"`

### 7.2 ticker

- **quote 必須**
- **4 文字の英数字文字列**として扱う(先頭 0 落ち防止、英字組入れ対応)
- 例: `"7203"`, `"0036"`, `"130A"`

### 7.3 時価総額 / 売買代金

- 単位を明示(`oku` = 億円、`million` = 百万円)
- 例: `market_cap_oku: 1500`, `avg_turnover_oku: 5.2`

### 7.4 null 許容

- 取得不能・未公表フィールドは明示的に `null`
- 省略(キー自体を書かない)は避ける(schema 検証時に欠損と区別困難)

### 7.5 配列

- 参照は repository ref: `macro_context_ref`, `candidate_ref.candidates_ref`(research では `candidate_ref.ticker` と組み合わせて candidates row に一致させる)

## 8. 運用

- 各 `records/04-candidates/YYYY/MM/YYYY-MM-DD.yaml` の `universe_size` は scope(全普通株 + bar 履歴条件)の銘柄数を記録する
- `selection.liquidity` の閾値変更は月次 retro の議題とし、変更前に replay / ablation で計測する

### JPX 規制情報の取得失敗

- 特別注意 / 整理 / 取引停止 / 上場廃止警告の参照に必要な JPX 公開情報(CSV / Excel / HTML)が取得できない run は、**fail-fast** として `candidates` を生成しない
- `jpx_flags` 事実と selection の除外判断に直結するため、`unknown` 扱いで run 継続しない
- 特別注意銘柄は `JPX_SPECIAL_CAUTION_INDEX_URL` があれば、日次変動する個別銘柄信用取引残高表の `mtdailyk*.xls` を index から解決する
- JPX 規制情報は latest snapshot 取得のため、7 weekday 超のバックフィルでは通常 fail-fast する。`screening run` は JPX を取得しないため、例外運用では先に `bootstrap-cache --asof` で SQLite に保存し、`verify-cache-coverage --allow-stale-jpx` と `run --allow-stale-jpx` で明示的に許容する

## 9. 参考

- [`principles.md`](./principles.md): スクリーニング原則
- [`valuation-metrics.md`](./valuation-metrics.md): 指標算出仕様
- [`mechanical.md`](./mechanical.md): 機械的ふるい仕様
- [`../components/candidates.md`](../components/candidates.md): candidates 運用仕様
