# screening/principles.md

Baibai-Loop のスクリーニングサブシステムの設計原則。Candidates、macro context、investment memo のフローに対応するルール集。全体構造は [`../architecture/system-overview.md`](../architecture/system-overview.md)、概念モデルは [`../concepts.md`](../concepts.md) を参照。

## 1. Decision lifecycle との接続

| Lifecycle artifact | スクリーニング側の対応 | この原則集での位置付け |
| --- | --- | --- |
| `records/04-candidates/` | 機械的ふるい | [`mechanical.md`](./mechanical.md) で仕様化 |
| `records/01-macro-context/` | screening 前の macro context | [`../components/macro-context.md`](../components/macro-context.md) |
| `records/05-research/` | Playbook + thesis payoff + 採用判定 | 本ファイル + Playbook 本体 |

## 2. Macro Context

- Macro context は hard gate ではなく、screening / research の確認観点として使う。
- Validator-visible な sizing cap は portfolio policy と research 判断で扱う。

## 3. Playbook 定義（概要）

詳細は `records/_playbooks/` 本体を参照。screening では playbook-linked screen と playbook を 1 対 1 で対応させ、retro でどの thesis pattern が機能したかを分けて検証する。

| playbook | primary evidence path | 狙い |
| --- | --- | --- |
| `valuation-reversion` | PER / PBR / exact かつ正の EV/EBITDA の相対割安、短期急落、sector rotation | 伝統的な valuation mean-reversion |
| `strict-net-cash-discount` | EDINET cash - debt / market cap と Eq / market cap | 有利子負債を差し引いても財務余力が厚い asset discount 候補 |
| `fcf-yield-discount` | EDINET CFO - capex / market cap | 設備投資後の現金創出力に対して安い候補 |
| `cash-rich-asset-discount` | CashEq / market cap と Eq / market cap の厚さ | J-Quants summary で拾える cash-rich / asset discount 候補。ただし EDINET net debt が取れる場合は抑止 |
| `cashflow-yield-discount` | 期間正規化した CFO TTM / market cap | PER では拾いにくい現金創出力の割安 |
| `sales-discount-growth` | P/S discount + 売上成長維持 | 利益が薄いが売上成長が残る調整銘柄 |

単一総合 score は持たせない。現行 candidates YAML では `evidence_hits[]` を lane 順に記録するが、概念上は playbook-linked evidence hit として扱う。Research では primary playbook 1 つと supporting evidence を分けて扱う。

### 3.1 Selection lens

Playbook-linked screen は raw candidates を作る事実層、`select` は research recommendations を作る triage 層として分ける。`select` は以下の lens を使う。

| lens | 目的 | 採用根拠としての扱い |
| --- | --- | --- |
| `fast_dislocation` | 一時的に売られすぎた候補を上位化しやすくする | 価格下落 trigger と fundamental guard が必要。出来高 spike / 52 週安値距離は補助情報 |
| `long_hold_survivability` | 短期 thesis が外れた場合の保有耐性を見える化する | hard gate ではない。`high|medium|low|unknown` annotation |
| `prior_research` | deferred / rejected の再登場を抑制し、同じ候補に偏る問題を下げる | ledger の revisit_after / expires_at を尊重 |

Selection profile の built-in は `balanced` のみとする。built-in の選択肢は、profile 間の優劣が forward 計測で示されない限り増やさない。custom profile は `select-sweep --profile-config` の YAML で比較する。運用閾値を変える前に複数 asof の実データ replay と hold-out 確認を行い、typo や補助 trigger だけの fast-dislocation を fail-fast / ineligible にする。

## 4. 4 軸評価（単一総合点に戻さない）

Research packet で以下の 4 軸を記入する。**合計点は算出しない**:

| 軸 | 評価対象 | 記録形式 |
| --- | --- | --- |
| Valuation | PER / PBR / EV-EBITDA / P-S / PCFR / OCF yield / cash-to-market-cap | 指標ごとに値、比較対象、primary metric |
| Mean-Reversion | 急落有無 / 自己過去レンジ下位度 / セクターローテーション起因度 | 定量値 + 1-2 行コメント |
| Catalyst | 有無 / freshness / 種別 | 種別 + 経過営業日 + 一次ソース URL |
| Positioning / liquidity | 空売り残高 / 日々公表信用 / 特別注意 / 貸借状態 / 出来高 | 各指標の絶対値 + 60 日推移 |

各軸に **寄与度 3 段階**（strong / weak / neutral）を記録し、retro で軸別 bias を定性分析する。

### 4.1 なぜ単一 score に戻さないか

- 候補数が少ない段階では、統計的に weight 調整する根拠データが足りない
- 単一 score は「なぜ選んだか」を失い、feedback loop での学習信号を劣化させる
- 4 軸表 + 寄与度 + primary metric なら、後から playbook 改訂時に軸ごとの bias を定性的に再分類できる

## 5. 原因仮説 + 反対仮説必須

### 5.1 割安の原因仮説

- 市場全体の短期売り
- 業種ローテーションの一過性
- 一過性の悪材料
- 利益率低下・投資先行による短期的な見栄え悪化
- ネットキャッシュ / 資産価値 / CF 創出力の見落とし
- インデックス構成変更・需給要因

### 5.2 反対仮説 - 構造的理由（全 packet 必須）

1. 構造的な成長鈍化
2. ガバナンス懸念
3. 技術的陳腐化
4. accounting 警戒
5. 業界需要の構造的縮小
6. ESG / 規制リスク
7. 大株主の売り圧力
8. 営業 CF の一過性要因
9. 有利子負債・偶発債務
10. その他（自由記述）

四半期 retro で自由記述を読み返し、再分類候補を作る。

## 6. Kill Switch

以下の状況では entry 不可:

- **決算発表日またぎエントリー禁止**
- **日銀金融政策決定会合の前日エントリー禁止**
- **FOMC 前日エントリー禁止**
- **macro context が stale / future のまま理由なしに `proceed` すること**。stale で採用する場合は entry preflight で `starter` / `defer` / `exception` とし、macro 更新、低 sizing、near-term catalyst、低相関理由のいずれかを明記する。future macro context は approved 不可。

保有中に macro context が変わった場合、exit / sizing 見直しの必要性を research / review で確認する。

## 7. Position sizing

position は **paper proxy layer (1 億円仮想資本)** と **real layer (実資金)** の 2 つの観点で管理する。research / trade record では両者を別 field に記録し、validator も別 rule でチェックする (詳細は [`../components/trades.md`](../components/trades.md))。

### 7.1 Paper proxy layer

| 条件 | 許容 position | 備考 |
| --- | --- | --- |
| single primary evidence path | 最大 1% | 標準 |
| 複数 independent evidence paths | 最大 2% | 複数の独立した割安根拠が重なる場合のみ |

`adv_participation_pct >= 5.0` は hard reject。現在の実資金や tactical cap が小さい場合、paper proxy の ADV cap は実運用ではほぼ拘束しないため、検証用の統一尺度として扱う。

### 7.2 Real layer

実資金で執行する場合、paper proxy と独立した集中度ルールを満たす。実資金最低投入単位によって soft 推奨を超えることがあり、その場合は本文で「最低投入単位による不可避な超過」を明記する。

`real_capital_yen` は投資可能な実資金全体を分母にする。当面の様子見枠・イベント前の一時的な投入上限を置く場合は、`real_capital_yen` を小さくせず、trade record の `tactical_real_budget_yen` / `tactical_real_budget_concentration_pct` に分けて記録する。real concentration は破滅的な単一銘柄集中の管理、tactical real budget concentration は今どこまでリスクを取りに行くかの timing 管理として扱う。

| 区分 | soft 推奨 | hard 上限 (`overrides` 必須) |
| --- | --- | --- |
| 単一銘柄集中度 (`real_concentration_pct`) | < 25% | 50% |
| 単一 sector_33 集中度 | < 40% | 60% |
| cash 比率 | > 30% | 最低 10% |

## 8. Universe

- 時価総額 100 億円以上
- 20 営業日平均売買代金 1 億円以上
- 除外: ETF / REIT / 優先株 / 上場 182 日未満 / 特別注意 / 整理銘柄 / 取引停止 / 上場廃止警告
- universe は単一化し、Core / Exploratory / Watch-only は持たない

詳細: [`universe-rules.md`](./universe-rules.md)

## 9. AI の役割境界

`../components/research.md` の「AI の役割境界（packet 項目単位）」節を参照。核心:

- **AI 可**: Thesis / valuation snapshot / 仮説ドラフト / catalyst ドラフト / price reaction / positioning / liquidity 取得 / 株主還元確認ドラフト / evidence 寄与度初期評価
- **人間のみ**: Macro context の前提確認 / 一次ソース URL 確認 / 最終採用判定 / 失敗分類確定

## 10. 参考

- [`../philosophy.md`](../philosophy.md): 思想（macro context discipline、事実と分析の分離、feedback loop 先行、markdown 駆動）
- [`../architecture/system-overview.md`](../architecture/system-overview.md): 全体構造
- [`../components/research.md`](../components/research.md): research 運用仕様
- [`universe-rules.md`](./universe-rules.md): universe 境界条件
- [`valuation-metrics.md`](./valuation-metrics.md): 指標算出仕様
- [`mechanical.md`](./mechanical.md): 機械的ふるい仕様
- [`../components/macro-context.md`](../components/macro-context.md): Macro context contract
- [`/records/_playbooks/`](/records/_playbooks/): playbook 本体
