# 設計原則

Baibai-Loop の運用上の設計原則を記述する。本原則は [`philosophy.md`](./philosophy.md) の 4 つの柱を具体運用に落とした実践ルールである。構造・schema は [`architecture/system-overview.md`](./architecture/system-overview.md)、日々の運用手順は [`operations/README.md`](./operations/README.md) を参照。

## 1. 4 成分 + 下流アーキテクチャを前提とする

Baibai-Loop は **4 成分 (`records/01-brief/`, `records/03-candidates/`, `records/02-outlook/`, `records/04-research/`) + 下流 (`records/05-trades/`, `records/06-reviews/`)** の構造で運用する。全ての設計判断は本アーキテクチャを前提とする。詳細は [`architecture/system-overview.md`](./architecture/system-overview.md)。

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

### 4.5 分析レイヤーにプロセス指示を書かない

分析レイヤー（outlook, research）は判断と根拠を残す場所であり、運用手順そのものを書く場所ではない。
特に `records/02-outlook/` の `rationale` / `changes.rationale` には、業種・地域見解の根拠だけを書く。
「research では会社IRを確認する」「次回からこの手順で調べる」のようなプロセス指示は
`docs/components/`、`docs/operations/`、`docs/anti-patterns.md` に置く。

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

## 9. 非バックテスト原則（forward-only decision-support）

本リポジトリは **過去データへのパラメータ最適化** や **戦略累積リターンのシミュレーション**
を行わない。screening 閾値・playbook 採用条件・position sizing は人間が原則ベースで決め、
過去データに対する fit や grid search で update しない。

### 9.1 やらないことの一覧

- **バックテスト**: 過去 N 年に対する累積リターン・MaxDD・シャープ計算をしない
- **パラメータ・サーチ**: `sector_median_gap < -20%`、`self_range bottom 20%` 等の閾値を
  grid search で fit しない (固定値の恣意性は受け入れる)
- **生存者調整 / look-ahead 補正のシミュレーション**: backtest をしないので necessitate
  しない。ただし J-Quants 銘柄 master / 価格調整係数 / JPX 規制データは latest-snapshot
  で取得しており、完全な PIT snapshot ではない点はデータ層の限界として残る (詳細は
  [`reference/data-sources.md`](./reference/data-sources.md) §「取得データの保存方針」)
- **戦略パフォーマンスの track record claim**: 「過去 X 年で年率 Y%」のような report を
  作らない
- **アルファ / ベータ / シャープ等の事前計測**: 入る前に「どれだけ稼いだか」を計らない

### 9.2 代わりにやること（forward-only）

- forward-only な ledger 蓄積 (`records/_ledger/` の paper trade 記録、entry 後の前進的 P/L)
- 事前 thesis の文書化 (`records/04-research/`) と事後検証 (`records/06-reviews/`) の対比
- 月次 retro でのプロセス改善 (playbook 改訂は **サンプル数 10 件以上** を条件に検討)

### 9.3 根拠

- 1 名運用・記録駆動なので、backtest を組めるほどの過去サンプルが入手しにくい (J-Quants
  Light の rate limit、EDINET の point-in-time 取得制約等)
- 過去最適化を始めると **データスヌーピング** に陥り、playbook が「過去 fit に向かう」
  pressure に逆らえない。原則ベースで思想を固定し、forward-only で realistic な
  performance を観測して playbook を改訂する方が長期 robust
- バックテスト前提のレビュー指摘 (サバイバビリティ・バイアス、look-ahead bias、データ
  スヌーピング、ベンチマーク比較不在等) は本原則を採用している限り **原則として該当
  しない** (= 過去累積リターンの算出を行わないので発生する余地がない)。レビューを
  受けた際は本 section を参照する。なお、データ層では完全な PIT snapshot を保有して
  いない (前項 9.1 末尾参照) ため、forward-only であることは backtest 品質を保証する
  ものではなく、survivorship-correct backtest がスコープ外であることを意味する

## 10. 参考

- [`philosophy.md`](./philosophy.md): 思想・ベース概念・4 つの柱
- [`architecture/system-overview.md`](./architecture/system-overview.md): 構造・schema・procedure
- [`operations/README.md`](./operations/README.md): 日々の運用ワークフロー
- [`reference/data-sources.md`](./reference/data-sources.md): データソース
