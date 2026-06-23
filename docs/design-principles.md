# 設計原則

Baibai-Loop の運用上の設計原則を記述する。本原則は [`philosophy.md`](./philosophy.md) の 4 つの柱を具体運用に落とした実践ルールである。構造・schema は [`architecture/system-overview.md`](./architecture/system-overview.md)、日々の運用手順は [`operations/README.md`](./operations/README.md) を参照。

## 1. 2 つのループを前提とする

Baibai-Loop は **運用ループ**（運用方針 → マクロ環境分析 → 機械スクリーニング → 通過銘柄リスト → リサーチ候補選定 → 個別銘柄リサーチ → 売買提案 → 〔人間判断〕→ 売買執行記録）と **改善ループ**（全候補 forward-only backtest ＋ 設計レビュー ＋ trades の Q2 執行信号 → GitHub Issue の改善バックログ → screening rules / playbooks / config の改訂）の 2 つを分けて運用する。全ての設計判断はこの 2 ループと責務境界を前提とする。詳細は [`concepts.md`](./concepts.md) と [`architecture/system-overview.md`](./architecture/system-overview.md)。

## 2. 分析階層: 世界情勢 → 地域経済 → 個別資産

Macro context（`records/01-macro-context/`）における調査は、以下の階層で上から順に分析する:

1. **世界情勢**: グローバルマクロ・主要中央銀行・コモディティ・地政学
2. **地域経済**: 対象資産が属する地域の一次統計・金融政策・為替
3. **個別資産**: 対象資産のマーケット指標・セクター動向・個別イベント

主対象資産は日本株のため、具体的には「世界情勢（グローバル）→ 日本経済 → 日本株」となる。

### 根拠

- **因果の向きに忠実**: マクロ経済の因果連鎖は「グローバル → 地域 → 個別」の順に伝播することが多い（例: FOMC 金利判断 → USD/JPY → 輸出関連の日本株）。逆順では説明できないケースが多い
- **日本株バイアスの回避**: 世界情勢を分析せずに日本株だけを見ると、日本固有要因と海外要因の切り分けができない。階層化により「どの層で起きた変化が日本株に波及したか」を構造的に追える

## 3. 更新サイクルとファイル粒度の一致

Macro context は、screening 前に既存 context が stale / scope mismatch / premise break の場合だけ更新する。定期作成を目的化せず、判断前提の鮮度を保つために作る。

外部記事や統計値は source として使うが、記事本文や網羅的な時系列 fact を repo に蓄積しない。ローカルに残すのは screening / research の前提として再利用する macro view と、その view を作るために参照した source metadata だけでよい。

## 4. 事実と分析の分離（philosophy 柱 1 の具体化）

事実層（candidates）と分析層（macro context, research）を物理的に別ファイル/別ディレクトリに分離する。同一ファイルに混在させない。

### 4.1 ファイル単位の分離

| レイヤー | 扱う対象 | 格納先 | lifecycle role |
|---|---|---|---|
| Security-level 事実 | スクリーニング通過銘柄・valuation 指標 snapshot | `records/04-candidates/` 配下 | screen output |
| マクロ分析 | 外部記事・統計を踏まえた screening 前提、業種/地域の追い風/中立/逆風評価 | `records/01-macro-context/` 配下 | macro context |
| Security-level 分析 | 個別銘柄の深掘り・原因仮説・反対仮説・採用判定 | `records/05-thesis/` 配下 | investment memo |

### 4.2 事実レイヤー（candidates）に含めてよいもの

- Valuation 指標の算出結果（`records/04-candidates/` 側）

### 4.3 事実レイヤーで禁止するもの

- 「〜を示唆する」「〜を受けて」「〜を背景に」等の因果推論表現
- 「次の FOMC では〜が予想される」等の予測
- 「この動きは〜を意味する」等の意味付け
- 「注目すべき」「重要な」等の重要度評価（Major/Notable は「変化量の統計的大きさ」のラベルであり、重要度評価ではない）

### 4.4 用語の運用ルール

事実レイヤー（candidates）で使う用語は、解釈を招かない中立的なものを選ぶ:

- 「連続トレンド」「転換点」のような解釈を帯びる語は使わない
- 「方向履歴」「方向反転」のように機械的計算結果として中立な語を使う

新しい用語を導入する際は「自然言語として解釈や予測を含意しないか」をチェックする。

### 4.5 分析レイヤーにプロセス指示を書かない

分析レイヤー（macro context, research）は判断と根拠を残す場所であり、運用手順そのものを書く場所ではない。
特に `records/01-macro-context/` には、screening 前提として使う macro view と source metadata だけを書く。
「research では会社IRを確認する」「次回からこの手順で調べる」のようなプロセス指示は
`docs/components/`、`docs/operations/`、`docs/anti-patterns.md` に置く。

## 5. Macro Context Discipline（philosophy 柱 2 の具体化）

- Macro context は hard gate ではなく、screening / research の優先順位、追加確認、sizing caution を決める判断前提として扱う
- `records/05-thesis/` の採用判定では `records/01-macro-context/` との fit を必ず確認する

## 6. Feedback loop 先行の原則（philosophy 柱 3 の具体化）

- 完成設計より不完全な loop 1 周を優先
- サンプル数 10 件未満なら playbook 据え置きを許容する

## 7. Markdown / YAML 駆動の原則（philosophy 柱 4 の具体化）

- 投資判断 record は DB / Feature Store 化しない
- provider 由来の再生成可能な input/cache は SQLite に閉じ、判断 record とは分離する
- front matter（YAML）を揃え、script での後付け抽出を可能にする
- Git で diff / blame / history を追跡可能にする

## 8. レイヤー分離の運用

- 「世界情勢」レイヤーの指標は、日本固有バイアスを含めない
- 「日本経済」レイヤーでは、世界情勢レイヤーで既に扱った指標（例: 米 10Y 利回り）を再掲しない
- 「日本株」レイヤーでは、日本経済レイヤーで既に扱った為替・金利を再掲しない

再掲を避けることで、同じ情報を複数箇所で管理するコストと矛盾リスクを減らす。

## 9. 計測の原則：forward-only な backtest と、避ける最適化

screening（playbook / lens / regime / 閾値）の効果は、過去週を look-ahead を排して replay する
**forward-only な multi-axis backtest** で検証する。手順の正本は
[`operations/backtest-runbook.md`](./operations/backtest-runbook.md) の 7 axis
（screening-replay / playbook-cohorts / selection-ablation / judgment-gate counterfactual /
bootstrap CI / opportunity-cost / regime×playbook）であり、playbook の追加・削除、playbook 順、
regime lens の有効化などはこの計測を根拠に discrete に改訂する。

一方、screening 閾値・playbook 採用条件・position sizing の **値そのもの** は人間が原則ベースで
固定し、過去データへの fit や grid search では update しない。backtest は「固定した仕組みが
forward でどう効いたか」を測るためのものであって、「過去に最も効いた値を探す」ためのものではない。
この線引きが forward-only 規律の本体である。

### 9.1 backtest でやること（forward-only）

- screening の multi-axis backtest (`backtest-runbook` の 7 axis)。判断時点 (asof) に存在した
  情報のみ使い、in-sample / out-of-sample を分けて計測する
- forward-only な decision register 蓄積 (`records/_decisions/` の判断イベント、entry 後の前進的 attribution)
- 事前 thesis の文書化 (`records/05-thesis/`) と事後 fill/exit (`records/06-position/`) の対比
- 月次 forward 計測でのプロセス改善 (playbook 改訂は **サンプル数 10 件以上** を条件に検討)

### 9.2 やらないこと（避ける最適化・claim）

- **未来データを用いた閾値 grid search / パラメータ最適化**: `sector_median_gap < -20%`、
  `self_range bottom 20%`、regime 閾値 ±3% 等は事前に固定値で登録する (固定値の恣意性は
  受け入れる)。観測後に「最も効いた値」へ最適化しない (データスヌーピング回避)
- **戦略累積リターンの track record claim**: 「過去 X 年で年率 Y%」「MaxDD・シャープ」のような
  cumulative performance report / 事前のアルファ・ベータ計測を作らない
- **短期トレード単位の累積 backtest**: trade は forward-only に decision register と fill/exit を
  対比する。判断 gate の効果は judgment-gate counterfactual (backtest axis D) で個別に測る
- **生存者調整 / look-ahead 補正のシミュレーション**: cumulative-return backtest をしないので
  necessitate しない。ただし J-Quants 銘柄 master / 価格調整係数 / JPX 規制データは
  latest-snapshot 取得であり、完全な PIT snapshot ではない点はデータ層の限界として残る (詳細は
  [`reference/data-sources.md`](./reference/data-sources.md) §「取得データの保存方針」)

### 9.3 根拠

- screening の仕組み (どの playbook / lens が forward return を生んだか) は計測しないと改善できない。
  柱 5「計測ファースト」に従い、multi-axis backtest で価値を実証した施策だけを採用し、発動ゼロの
  機能を ablation で棚卸しする
- だが 1 名運用・記録駆動では、閾値を過去に fit できるほどの独立サンプルが入手しにくく (J-Quants
  Light の rate limit、EDINET の point-in-time 取得制約等)、過去最適化を始めると
  **データスヌーピング** で「過去 fit に向かう」pressure に逆らえない。値は原則ベースで固定し、
  forward-only backtest で realistic な効果を観測する方が長期 robust
- multi-axis backtest を行う以上、look-ahead bias・データスヌーピング・ベンチマーク比較不在・
  小サンプルといったレビュー指摘は **本基盤にも当てはまる**。これらは backtest-runbook の規律
  (look-ahead 排除・in/out-of-sample 分離・benchmark proxy 比・bootstrap CI・固定閾値) で扱う。
  なお完全な PIT snapshot は保有しない (9.2 末尾) ため、forward-only は survivorship-correct な
  cumulative backtest がスコープ外であることを意味する

## 10. 参考

- [`philosophy.md`](./philosophy.md): 思想・ベース概念・4 つの柱
- [`architecture/system-overview.md`](./architecture/system-overview.md): 構造・schema・procedure
- [`operations/README.md`](./operations/README.md): 日々の運用ワークフロー
- [`reference/data-sources.md`](./reference/data-sources.md): データソース
