# screening/principles.md

Baibai-Loop のスクリーニングサブシステムの設計原則。4 成分アーキテクチャの `(b) candidates` + `(c) outlook` + `(d) research` のフローに対応するルール集。全体構造は [`../architecture.md`](../architecture.md) を参照。

## 1. 4 成分アーキテクチャとの接続

スクリーニングサブシステムは、4 成分のうち以下に対応する:

| 成分 | スクリーニング側の対応 | この原則集での位置付け |
| --- | --- | --- |
| (b) `records/03-candidates/` | 機械的ふるい | [`mechanical.md`](./mechanical.md) で仕様化 |
| (c) `records/02-outlook/` | Macro gate の source（簡易版） | [`macro-gate-procedure.md`](./macro-gate-procedure.md) で手順化 |
| (d) `records/04-research/` | Playbook + 4 軸評価 + 採用判定 | 本ファイル + Playbook 本体 |

## 2. マクロ優位 (76/24) 原則

- **マクロ 76% / ミクロ 24%** の比重（philosophy 柱 2）
- Macro gate を通過しない銘柄は採用不可（research 段階で除外）
- gate 判定は outlook → research の接続で行う（[`macro-gate-procedure.md`](./macro-gate-procedure.md)）

## 3. Playbook P-A / P-B 定義（概要）

詳細は `records/_playbooks/valuation-*.md` 本体を参照。ここでは概要のみ。

### 3.1 P-A: Valuation Mean-Reversion（本命）

- **対象**: 数値 valuation が業種中央値・過去自己比較で **一時的に割安** と判定される銘柄
- **判定条件**: 閾値 3 種のうち **最低 1 つ満たす**（OR 条件、[`mechanical.md`](./mechanical.md)）
- **狙い**: 市場の短期過剰売りによる底値を掴む
- **保有期間**: 5〜40 営業日

### 3.2 P-B: Valuation + Catalyst Confirmation（補助）

- **対象**: P-A の valuation 条件 + 短期 catalyst（決算修正・自社株買い・東証開示等）
- **判定条件**: P-A 条件 + catalyst freshness ≦ 60 営業日
- **狙い**: 割安銘柄に再評価トリガーが重なるケース
- **保有期間**: 5〜40 営業日

## 4. 4 軸評価（単一総合点に戻さない）

Research packet で以下の 4 軸を記入する。**合計点は算出しない**:

| 軸 | 評価対象 | 記録形式 |
| --- | --- | --- |
| Valuation | PER / PBR / EV-EBITDA / P-S / PCFR | 指標ごとに `value / 業種中央値 / 過去3年パーセンタイル` + primary metric |
| Mean-Reversion | 急落有無 / 自己過去レンジ下位度 / セクターローテーション起因度 | 定量値 + 1-2 行コメント |
| Catalyst | 有無（2 値）/ freshness（営業日）/ 種別 | 種別 + 経過営業日 + 一次ソース URL |
| Crowding | 空売り残高 / 日々公表信用 / 特別注意 / 貸借状態 | 各指標の絶対値 + 60 日推移 |

各軸に **寄与度 3 段階**（strong / weak / neutral）を記録し、retro で軸別 bias を定性分析する。

### 4.1 なぜ単一 score に戻さないか

- 候補数が月 10-15 件 / 採用 3 件規模では、統計的に weight 調整する根拠データが足りない
- 単一 score は「なぜ選んだか」を失い、feedback loop での学習信号を劣化させる
- 4 軸表 + 寄与度 + primary metric なら、後から playbook 改訂時に軸ごとの bias を定性的に再分類できる

## 5. 原因仮説 + 反対仮説必須

### 5.1 一時的割安の原因仮説（P-A 必須、P-B もできれば）

- 市場全体の短期売り
- 業種ローテーションの一過性
- 一過性の悪材料
- インデックス構成変更
- 需給要因の一時的売り

### 5.2 反対仮説 - 構造的理由（全 packet 必須）

8 例示 + 自由記述:

1. 構造的な成長鈍化
2. ガバナンス懸念
3. 技術的陳腐化
4. accounting 警戒
5. 業界需要の構造的縮小
6. ESG / 規制リスク
7. 大株主の売り圧力
8. その他（自由記述）

四半期 retro で自由記述を読み返し、再分類候補を作る。

## 6. Kill Switch

以下の状況では entry 不可:

- **決算発表日またぎエントリー禁止**（保有期間内に決算発表日が入る）
- **日銀金融政策決定会合の前日エントリー禁止**
- **FOMC 前日エントリー禁止**
- **マクロゲートが `headwind` の銘柄**（outlook で headwind 判定）
- **outlook が未作成ならマクロゲート判定不能なので entry 不可**

保有中に outlook が更新され gate が `headwind` に転じた場合、即時 exit 検討。

## 7. Position sizing（時価総額別上限）

| 時価総額 | 許容 position | 備考 |
| --- | --- | --- |
| 1,000 億円以上 | 最大 2% | 標準 |
| 500〜1,000 億円 | 最大 1% | |
| 200〜500 億円 | 最大 0.5% | **P-B のみ**、catalyst freshness ≦ 10 営業日 + 出来高 1.5x 以上 |

## 8. Universe

- 時価総額 200 億円以上
- 20 営業日平均売買代金 3 億円以上
- 除外: ETF / REIT / 優先株 / 上場 6 か月未満 / 特別注意 / 整理銘柄
- 上場 3 年未満の扱い: 上場来レンジで代替 or P-B 限定

詳細: [`universe-rules.md`](./universe-rules.md)

## 9. AI の役割境界

`../components/research.md` の「AI の役割境界（packet 項目単位）」節を参照。核心:

- **AI 可**: Thesis / valuation snapshot / 仮説ドラフト / catalyst ドラフト / price reaction / crowding 取得 / 4 軸寄与度初期評価
- **人間のみ**: Macro gate 判定確定 / 一次ソース URL 確認 / 最終採用判定 / 失敗分類確定

## 10. 参考

- [`../philosophy.md`](../philosophy.md): 思想（マクロ優位、事実と分析の分離、feedback loop 先行、markdown 駆動）
- [`../architecture.md`](../architecture.md): 全体構造
- [`../components/research.md`](../components/research.md): research 運用仕様
- [`failure-taxonomy.md`](./failure-taxonomy.md): 失敗分類詳細
- [`universe-rules.md`](./universe-rules.md): universe 境界条件
- [`valuation-metrics.md`](./valuation-metrics.md): 指標算出仕様
- [`mechanical.md`](./mechanical.md): 機械的ふるい仕様（閾値 3 種 OR）
- [`macro-gate-procedure.md`](./macro-gate-procedure.md): Macro gate 判定手順
- [`/records/_playbooks/valuation-mean-reversion-v1.md`](/records/_playbooks/valuation-mean-reversion-v1.md): P-A 本体
- [`/records/_playbooks/valuation-catalyst-confirmation-v1.md`](/records/_playbooks/valuation-catalyst-confirmation-v1.md): P-B 本体
