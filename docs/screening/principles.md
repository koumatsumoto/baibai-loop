# screening/principles.md

Baibai-Loop のスクリーニングサブシステムの設計原則。4 成分アーキテクチャの `(b) candidates` + `(c) outlook` + `(d) research` のフローに対応するルール集。全体構造は [`../architecture/system-overview.md`](../architecture/system-overview.md) を参照。

## 1. 4 成分アーキテクチャとの接続

| 成分 | スクリーニング側の対応 | この原則集での位置付け |
| --- | --- | --- |
| (b) `records/03-candidates/` | 機械的ふるい | [`mechanical.md`](./mechanical.md) で仕様化 |
| (c) `records/02-outlook/` | Macro gate の source | [`macro-gate-procedure.md`](./macro-gate-procedure.md) で手順化 |
| (d) `records/04-research/` | Playbook + 4 軸評価 + 採用判定 | 本ファイル + Playbook 本体 |

## 2. マクロ優位 (76/24) 原則

- **マクロ 76% / ミクロ 24%** の比重（philosophy 柱 2）
- Macro gate を通過しない銘柄は採用不可（research 段階で除外）
- gate 判定は outlook → research の接続で行う（[`macro-gate-procedure.md`](./macro-gate-procedure.md)）

## 3. Playbook 定義（概要）

詳細は `records/_playbooks/` 本体を参照。screening では signal lane と playbook を 1 対 1 で対応させ、retro でどの割安タイプが機能したかを分けて検証する。

| playbook | primary signal | 狙い |
| --- | --- | --- |
| `valuation-reversion` | PER / PBR / exact かつ正の EV/EBITDA の相対割安、短期急落、sector rotation | 伝統的な valuation mean-reversion |
| `strict-net-cash-discount` | EDINET cash - debt / market cap と Eq / market cap | 有利子負債を差し引いても財務余力が厚い asset discount 候補 |
| `fcf-yield-discount` | EDINET CFO - capex / market cap | 設備投資後の現金創出力に対して安い候補 |
| `cash-rich-asset-discount` | CashEq / market cap と Eq / market cap の厚さ | J-Quants summary で拾える cash-rich / asset discount 候補。ただし EDINET net debt が取れる場合は抑止 |
| `cashflow-yield-discount` | 期間正規化した CFO TTM / market cap | PER では拾いにくい現金創出力の割安 |
| `sales-discount-growth` | P/S discount + 売上成長維持 | 利益が薄いが売上成長が残る調整銘柄 |

単一総合 score は持たせない。候補 YAML では `signals[]` を lane 順に記録し、research では primary playbook 1 つと supporting signals を分けて扱う。

## 4. 4 軸評価（単一総合点に戻さない）

Research packet で以下の 4 軸を記入する。**合計点は算出しない**:

| 軸 | 評価対象 | 記録形式 |
| --- | --- | --- |
| Valuation | PER / PBR / EV-EBITDA / P-S / PCFR / OCF yield / cash-to-market-cap | 指標ごとに値、比較対象、primary metric |
| Mean-Reversion | 急落有無 / 自己過去レンジ下位度 / セクターローテーション起因度 | 定量値 + 1-2 行コメント |
| Catalyst | 有無 / freshness / 種別 | 種別 + 経過営業日 + 一次ソース URL |
| Crowding | 空売り残高 / 日々公表信用 / 特別注意 / 貸借状態 | 各指標の絶対値 + 60 日推移 |

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
- **マクロゲートが `headwind` の銘柄**（outlook で headwind 判定）
- **outlook が未作成ならマクロゲート判定不能なので entry 不可**

保有中に outlook が更新され gate が `headwind` に転じた場合、即時 exit 検討。

## 7. Position sizing

position は **paper proxy layer (1 億円仮想資本)** と **real layer (実資金)** の 2 つの観点で管理する。research / trade record では両者を別 field に記録し、validator も別 rule でチェックする (詳細は [`../components/trades.md`](../components/trades.md))。

### 7.1 Paper proxy layer

| 条件 | 許容 position | 備考 |
| --- | --- | --- |
| signal 1 つ | 最大 1% | 標準 |
| signal 2 つ以上 | 最大 2% | 複数の独立した割安根拠が重なる場合のみ |

`adv_participation_pct >= 5.0` は hard reject。現在の実資金が 100-200 万円程度の場合、paper proxy の ADV cap は実運用ではほぼ拘束しないため、検証用の統一尺度として扱う。

### 7.2 Real layer

実資金で執行する場合、paper proxy と独立した集中度ルールを満たす。実資金最低投入単位によって soft 推奨を超えることがあり、その場合は本文で「最低投入単位による不可避な超過」を明記する。

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

- **AI 可**: Thesis / valuation snapshot / 仮説ドラフト / catalyst ドラフト / price reaction / crowding 取得 / 株主還元確認ドラフト / 4 軸寄与度初期評価
- **人間のみ**: Macro gate 判定確定 / 一次ソース URL 確認 / 最終採用判定 / 失敗分類確定

## 10. 参考

- [`../philosophy.md`](../philosophy.md): 思想（マクロ優位、事実と分析の分離、feedback loop 先行、markdown 駆動）
- [`../architecture/system-overview.md`](../architecture/system-overview.md): 全体構造
- [`../components/research.md`](../components/research.md): research 運用仕様
- [`failure-taxonomy.md`](./failure-taxonomy.md): 失敗分類詳細
- [`universe-rules.md`](./universe-rules.md): universe 境界条件
- [`valuation-metrics.md`](./valuation-metrics.md): 指標算出仕様
- [`mechanical.md`](./mechanical.md): 機械的ふるい仕様
- [`macro-gate-procedure.md`](./macro-gate-procedure.md): Macro gate 判定手順
- [`/records/_playbooks/`](/records/_playbooks/): playbook 本体
