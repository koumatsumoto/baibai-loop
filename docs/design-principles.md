# 設計原則

Baibai-Loop の運用上の設計原則を記述する。本原則は [`philosophy.md`](./philosophy.md) の 4 つの柱を具体運用に落とした実践ルールである。構造・schema は [`architecture.md`](./architecture.md)、日々の運用手順は [`workflow.md`](./workflow.md) を参照。

## 1. 4 成分 + 下流アーキテクチャを前提とする

Baibai-Loop は **4 成分 (`records/01-brief/`, `records/03-candidates/`, `records/02-outlook/`, `records/04-research/`) + 下流 (`records/05-trades/`, `records/06-reviews/`)** の構造で運用する。全ての設計判断は本アーキテクチャを前提とする。詳細は [`architecture.md`](./architecture.md)。

## 2. 分析階層: 世界情勢 → 地域経済 → 個別資産

マクロ track（`records/01-brief/` → `records/02-outlook/`）における調査は、以下の階層で上から順に分析する:

1. **世界情勢**: グローバルマクロ・主要中央銀行・コモディティ・地政学
2. **地域経済**: 対象資産が属する地域の一次統計・金融政策・為替
3. **個別資産**: 対象資産のマーケット指標・セクター動向・個別イベント

主対象資産は日本株のため、具体的には「世界情勢（グローバル）→ 日本経済 → 日本株」となる。

### 根拠

- **因果の向きに忠実**: マクロ経済の因果連鎖は「グローバル → 地域 → 個別」の順に伝播することが多い（例: FOMC 金利判断 → USD/JPY → 輸出関連の日本株）。逆順では説明できないケースが多い
- **日本株バイアスの回避**: 世界情勢を分析せずに日本株だけを見ると、日本固有要因と海外要因の切り分けができない。階層化により「どの層で起きた変化が日本株に波及したか」を構造的に追える

## 3. 更新サイクルとファイル粒度の一致

データは更新サイクルごとに別ファイルに分離する:

- **週次更新**: マーケット指標・地政学速報は週次 brief (`world-weekly`) に記録
- **日次更新**: 週次まで待つと stale になる一次統計・会合日程・直近 fact は日次 brief (`world-daily`) に記録
- **月次更新**: CPI / 雇用統計などの月次統計は月次 brief (`macro-monthly`) に記録
- **イベント時**: FOMC / 日銀 / 主要指標発表は個別 kind で記録（`event` type）

週次/日次の brief は、原則として月次データを再掲せず **月次 brief への参照** で済ませる。例外として、`macro-monthly` がまだ閉じていない期間は `world-daily` が月次級の新規統計を一時的に保持してよい。更新頻度が違うデータを同じファイルに同居させると、毎週の大半が「先週と同じ値」の羅列になり、メンテナンス負荷が非対称に膨らむ。

## 4. 事実と分析の分離（philosophy 柱 1 の具体化）

事実層（brief, candidates）と分析層（outlook, research）を物理的に別ファイル/別ディレクトリに分離する。同一ファイルに混在させない。

### 4.1 ファイル単位の分離

| レイヤー | 扱う対象 | 格納先 | 4 成分対応 |
|---|---|---|---|
| マクロ事実 | グローバル/日本経済の観測値・一次統計引用・機械的計算 | `records/01-brief/` 配下 | (a) |
| ミクロ事実 | スクリーニング通過銘柄・valuation 指標 snapshot | `records/03-candidates/` 配下 | (b) |
| マクロ分析 | マクロ見解・業種/地域の追い風/中立/逆風評価 | `records/02-outlook/` 配下 | (c) |
| ミクロ分析 | 個別銘柄の深掘り・原因仮説・反対仮説・採用判定 | `records/04-research/` 配下 | (d) |

### 4.2 事実レイヤー（brief / candidates）に含めてよいもの

- 一次統計の数値引用（CPI 等）
- マーケット終値・利回り
- 前週比・前月比の計算結果
- workflow で明示された閾値ルールの適用結果（Major / Notable ラベル等）
- 過去 N 週の方向履歴（矢印列）
- 方向反転の機械的検出
- Valuation 指標の算出結果（`records/03-candidates/` 側）

### 4.3 事実レイヤーで禁止するもの

- 「〜を示唆する」「〜を受けて」「〜を背景に」等の因果推論表現（[`workflow.md`](./workflow.md) の禁止表現リスト参照）
- 「次の FOMC では〜が予想される」等の予測
- 「この動きは〜を意味する」等の意味付け
- 「注目すべき」「重要な」等の重要度評価（Major/Notable は「変化量の統計的大きさ」のラベルであり、重要度評価ではない）

### 4.4 用語の運用ルール

事実レイヤー（brief, candidates）で使う用語は、解釈を招かない中立的なものを選ぶ:

- 「連続トレンド」「転換点」のような解釈を帯びる語は使わない
- 「方向履歴」「方向反転」のように機械的計算結果として中立な語を使う

新しい用語を導入する際は「自然言語として解釈や予測を含意しないか」をチェックする。

## 5. マクロ優位 (76/24) の原則（philosophy 柱 2 の具体化）

- トレード判断の比重は **マクロ 76% / ミクロ 24%**
- 運用途中で動かさない
- `records/04-research/` の採用判定では `records/02-outlook/` の Macro gate 判定を必ず通す（gate を通らなければ採用不可）
- 詳細は [`screening/macro-gate-procedure.md`](./screening/macro-gate-procedure.md)

## 6. Feedback loop 先行の原則（philosophy 柱 3 の具体化）

- 完成設計より不完全な loop 1 周を優先
- `records/06-reviews/retro-YYYYMM.md` で playbook / screening 閾値の改訂判断を行う
- サンプル数 10 件未満なら playbook 据え置きを許容する

## 7. Markdown / YAML 駆動の原則（philosophy 柱 4 の具体化）

- DB / Feature Store を先行導入しない
- front matter（YAML）を揃え、script での後付け抽出を可能にする
- Git で diff / blame / history を追跡可能にする

## 8. レイヤー分離の運用

- 「世界情勢」レイヤーの指標は、日本固有バイアスを含めない
- 「日本経済」レイヤーでは、世界情勢レイヤーで既に扱った指標（例: 米 10Y 利回り）を再掲しない
- 「日本株」レイヤーでは、日本経済レイヤーで既に扱った為替・金利を再掲しない

再掲を避けることで、同じ情報を複数箇所で管理するコストと矛盾リスクを減らす。

## 9. 参考

- [`philosophy.md`](./philosophy.md): 思想・ベース概念・4 つの柱
- [`architecture.md`](./architecture.md): 構造・schema・procedure
- [`workflow.md`](./workflow.md): 日々の運用ワークフロー
- [`data-sources.md`](./data-sources.md): データソース
