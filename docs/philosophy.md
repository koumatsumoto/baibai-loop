# philosophy

Baibai-Loop が **何を信じ、なぜこの設計を選んだか** の正本。価値観・選択の根拠・却下した対立案を残す。
構造・schema・procedure は [`architecture/system-overview.md`](./architecture/system-overview.md) と [`architecture/README.md`](./architecture/README.md) に分離する。

このファイルは変更頻度が低い。運用で大きな信念が揺らいだときだけ更新する。日々の構造変更は [`architecture/`](./architecture/) 側で処理し、投資判断ドメインモデルは [`concepts.md`](./concepts.md) を正本とする。

## 1. Baibai-Loop の目的

Baibai-Loop は、日本株の実データを機械的に解析し、日本株スイングトレードの精度を売買反復で上げるための **データ解析基盤と意思決定ループ** である。

- 核心は「一時的に過剰に売られている割安銘柄を底値で掴んで exit する」
- 名称 "Baibai" = 売買、"Loop" = 反復学習サイクル
- 単発の売買記録ではなく、**売買を含む運用改善ループ** の基盤として設計する
- 基盤は **データ層 / 分析層 / 判断層の 3 層**(柱 5)で構成し、判断ループ(L3)が基盤の存在目的であり続ける

## 2. ベースの考え方（5 つの柱）

各柱は **(a) 信念 / (b) そう信じる根拠 / (c) 却下した対立案** の 3 段構成で記述する。

### 柱 1: 事実と分析の分離

#### (a) 信念

記録されるべき **事実**（`candidates`）と、人間/AI の **解釈**（`macro context`, `research`）は責務を分けて管理する。特に security-level の screening output と投資判断 memo を同一ファイルに混在させない。

#### (b) そう信じる根拠

- **コンテキスト汚染の回避**: AI エージェントが過去の journal を参照するとき、事実と意見が混在していると、AI が過去の解釈を「事実」として再生産する可能性がある。ファイル単位の分離で、「解釈ファイルを AI に見せない」選択ができる
- **後知恵バイアスの抑制**: 過去の事実ファイルを見返すとき、当時の解釈と事実を混同しない。解釈は retrospective に見直せる
- **帰責性**: retro で「事実誤認か解釈誤りか」の原因分析が分離できる
- **汚染の不可逆性**: 一度事実ファイルに解釈を混ぜると、どこまでが元の事実か判別困難になる。混ぜないのが最も安全

#### (c) 却下した対立案

- **タグで分ける**: front matter やインラインタグで「ここは事実、ここは解釈」とマークする案。解釈混入時にタグ漏れしやすく、チェックが機械化しにくい
- **front matter で `type: fact/analysis` を切り分け**: 同ファイル内で混在する構造がそもそも汚染を許す。ファイル単位で分けないと「解釈ファイルを AI に見せない」運用が成立しない
- **1 ファイル内で章節を分ける**: 事実節と解釈節の間に書き換えが混入するリスク。特に AI 支援ではファイル単位で touch される前提で考えるとリスクが高い

### 柱 2: Macro regime discipline

#### (a) 信念

トレード判断では、security-level の割安さだけでなく、macro / sector context を必ず確認する。

Validator-visible な採用可否と sizing cap は、portfolio policy config と research の構造 field が担う。Macro context は hard gate ではなく、screening / research の前提、優先 sector/theme、追加で確認すべき question を与える。

#### (b) そう信じる根拠

- **逆風下の割安は構造的 trap**: マクロ逆風業種の個別銘柄が割安でも、構造的に売られ続ける（valuation trap）。短期のリバウンドはあっても中期の戻りが期待できない
- **Top-down の合理性**: 世界情勢 → 日本経済 → 日本株 の階層で判断することは、マクロショックの transmission パスと一致している
- **底値狙いの性質**: 「一時的に過剰に売られている」の「過剰」を判定するには、マクロトレンドが追い風であることの確認が前提
- **実装可能性**: 注意配分は思想として固定し、実際の cap / sizing は policy config と research の構造 field で検査するほうが再現性が高い

#### (c) 却下した対立案

- **Macro を prose の注意喚起だけにする**: macro 逆風下の割安を拾う危険が残る。Macro context は research questions と fit 判定に落とし、position size は policy config と research overlay で制御する
- **Macro だけで候補を決める**: 個別銘柄の valuation、fundamental、catalyst、liquidity を活用しきれない。短期スイングには粗すぎる
- **動的 weight（機械学習による調整）**: candidate 数が 3 桁に満たない 1 人運用では、weight 学習の統計的根拠が出ない。学習データ不足で over-fit するリスク
- **比率を position sizing の数式にする**: attention policy と execution cap が混ざり、あとから sizing の妥当性を再検証しにくい

### 柱 3: Feedback loop 先行

#### (a) 信念

基盤設計が完成してから運用を開始するのではなく、**不完全でも loop を 1 周回してから改善** する。設計完成と運用開始の間に遅延を作らない。

#### (b) そう信じる根拠

- **運用による設計検証**: 実際に loop を回すと、不要と判明する設計要素が出てくる。先行精緻化すると YAGNI な設計に時間を投じることになる
- **MVP-first の精神**: 最小限で動くものを出し、運用のフィードバックで改善する、というソフトウェア開発の原則と整合
- **Retro の意味**: loop が回っていないと retro の材料がない。材料がないと改善の方向が見えない

#### (c) 却下した対立案

- **完成設計 → 運用開始**: 設計に時間をかけすぎて、実運用に届くまでに構造が陳腐化するリスク
- **Event Store / Feature Store の先行導入**: 1 人運用で YAGNI、schema migration コストが運用開始を遅らせる
- **設計凍結なしに実運用**: 構造なしに loop を回すと、何が記録されるべきか不定で混乱する。**最低限の lifecycle と front matter** で開始し、内容は育てる

### 柱 4: Markdown / YAML 駆動

#### (a) 信念

投資判断 record は **front matter が揃った Markdown と YAML** を正本にする。provider 由来の再生成可能な input/cache は SQLite に閉じ、判断・分析・運用 record は Git で読める形に保つ。

#### (b) そう信じる根拠

- **1 人運用の実行性**: 判断 record の DB 運用は個人で維持しきれない。ファイル + git で完結
- **Git との親和性**: diff / blame / history が標準ツールで扱える
- **AI 支援との親和性**: AI 下書き + 人間最終確認の協働が自然
- **後付け抽出の可能性**: front matter（YAML）が揃っていれば、script で集計・分析できる
- **vendor lock-in の回避**: Notion / Airtable 等は移行困難。Markdown + git は永続的

#### (c) 却下した対立案

- **投資判断 record / Feature Store の SQLite 化**: schema migration コストで運用開始が遅れる。1 人で判断 record の schema を進化させながら運用するのは負担大
- **JSON / YAML ファイル単独（Markdown なし）**: 人間の readability が低い。運用メモや解釈の記述に向かない
- **Notion / Airtable 等外部ツール**: vendor lock-in、git 統合困難、料金、AI 支援時の access 手続きなど運用課題が多い
- **最初から RDB + ORM**: 1 人運用の YAGNI 極致。運用で必要になるまで導入しない

### 柱 5: 計測ファーストのデータ基盤

#### (a) 信念

Baibai-Loop の主軸は、全上場銘柄の実データを保持する **データ層(L1)** と、決定論的な screen / lens / 軸別スコアとその forward 計測(replay / lane cohorts / ablation)からなる **分析層(L2)** である。判断層(L3 = records)はこの基盤の消費者であり、同時に計測 loop を閉じる ground truth を供給する。

用語の区別: L2 の「分析」は決定論的な機械処理を指し、その出力(candidates など)は柱 1 の意味では **事実** に属する(解釈を含まず再現可能なため)。柱 1 で「分析」と呼ぶのは人間/AI の解釈(macro context、research)であり、これは L3 に属する。

この柱は 4 つの規律を持つ:

1. **データ層は全上場銘柄を対象にする**。時価総額・流動性での絞り込みはデータを狭める理由にならず、分析時に適用するパラメータとして扱う
2. **計測経路のない機械的機能は追加しない**。新しい screen / lens / スコアは forward telemetry に接続できる形でだけ足し、計測で価値を示せなければ削除する
3. **スコアは軸別の座標であり判定ではない**。sector 相対・自己レンジ相対などの percentile / 相対値は機械的事実として出すが、単一の合成点や売買指示には決して畳まない
4. **AI が読む安定契約は CLI の YAML 出力と SQLite schema の 2 面に限る**。AI はこの 2 面から自由に事実を引き、grounded な下書きを作る。最終判断と帰責は人間に残る

#### (b) そう信じる根拠

- **エッジの出所の実測**: 運用上の成績改善は、裁量の精度より先に「裁量に渡す事実の品質」と「仕組みの計測可能性」への投資から生まれることを forward 計測で確認している(検証記録は `docs/screening/` の dated reference 群)
- **AI 協働の最適界面**: AI は機械可読な事実の合成は得意だが、最終判断の帰責はできない。正直な軸別事実 + 計測済みの仕組みという界面が、AI の強みを最大化し弱みを遮断する
- **削減の規律**: 「計測できること」を追加条件にすると、機能の価値が常に検証可能になり、ablation で発動ゼロの機能を機械的に棚卸しできる

#### (c) 却下した対立案

- **機械学習によるスコアリング**: サンプル数が 3 桁に満たない 1 人運用では overfit が必然で、判断の帰責性(なぜこの銘柄か)も壊れる。固定閾値 + forward 計測で十分に改善が回る
- **MCP server / API server 化**: single-operator・local-first の運用で YAGNI。CLI + SQLite 直接で AI は十分に使える。serving 層は保守コストだけが増える
- **リアルタイム化**: swing(5-40 営業日)の horizon に板情報や分足は不要。日次・週次バッチが品質検証(coverage fail-fast)と両立する
- **universe 事前絞り込みの維持**: fact 層を狭めると「分析の問いを変えるたびにデータ取得からやり直す」ことになる。データは広く保持し、絞り込みは分析時のパラメータとして適用する

## 3. なぜ lifecycle loop か（3 層の階層モデルではない）

Baibai-Loop は、単純な「事実 → 解釈 → 判断」の 3 層モデルではなく、portfolio policy から forward 計測 feedback までの lifecycle loop として扱う。

```text
portfolio policy
  -> macro context
  -> candidates
  -> research
  -> trades
  -> reports (forward 計測 / backtest)
  -> playbooks
  -> candidates
```

- **policy**: 人間向け document と、validator が読む `policy_config.py`。目的、制約、資本、許容リスク、time horizon を固定する
- **macro context**: screening 前に読む macro / sector 前提。外部記事・統計 series・人間/AI の判断を単一 artifact にまとめる
- **candidates**: security-level screen output。個別銘柄の候補事実を残す
- **research**: investment memo。macro context fit、個別 thesis、採用可否、position sizing を判断する
- **trades**: execution record。order / entry した判断がどう約定・保有・決済されたかを記録する
- **reports / playbooks**: `baibai-loop-ledger benchmark` / `screening-replay` / `lane-cohorts` / `selection-ablation` で forward 計測した結果を `reports/<asof>-*.md` に dated まとめとして残し、playbook feedback に戻す

階層的 3 層（事実 → 解釈 → 判断）だけだと、macro context、security-level thesis、execution、forward 計測 attribution が同じ「判断」層に混ざり、責務が重なる。Lifecycle loop として分けるほうが、どこで候補を拾い、どこで落とし、どこで改善するかを追いやすい。

なお柱 5 の L1/L2/L3 は **infrastructure の層**（データ / 機械的分析 / 判断の置き場所）であり、ここで退けている「判断プロセスの階層 3 層」とは別物である。lifecycle loop は L3 の中を流れ、L1/L2 はその全 stage に事実と計測を供給する。

## 4. なぜ 2 トラック（macro 独立 + 個別銘柄売買ループ）か

### (a) Macro context（必要時更新）

Macro context は **スクリーニング前に必要なら更新する**。CPI / BOJ / FOMC などの macro event 後、または候補銘柄が特定 sector に偏ったときに、外部記事・統計 series・AI/人間の判断をまとめて screening / research の前提にする。

### (b) 個別銘柄売買ループ

`candidates → research → trades → reports/playbooks` は **売買判断と連動する** ループ。screening 実行 → 選定 → 深掘り → 採用 → 執行 → forward 計測 (ledger CLI) → reports/playbooks feedback。

### (c) 2 トラックの統合点: research

research は:

- **入力**: 最新 candidates（security-level screen output）+ macro context + policy config
- **出力**: investment memo と採用可否、position sizing、execution への接続

macro context がなければ research の前提を確認できない。これは、macro / sector context なしに個別銘柄を評価しないという柱 2 の帰結である。

## 5. 用語選定の思想

主要 artifact の名前は、**役割を一語で表す** ことと **投資業界の慣習** を両立する。slug（英語名）は識別子として残し、人間向けには日本語概念名で呼ぶ。

| slug | 英語名 | 日本語概念名 | 採用理由 |
| --- | --- | --- | --- |
| `macro context` | macro context | マクロ環境分析 | screening 前に読む経済・市場・sector 前提をそのまま表す |
| `candidates` | screen output | 通過銘柄リスト | 機械的ふるいで残った銘柄群というデータの実体を直接表す |
| `research` | investment memo | 個別銘柄リサーチ | 業界標準の memo 形式に寄せつつ、repository path としては research を維持できる |
| `trades` | execution record | 売買執行記録 | trade / order / fill / cancellation を execution layer として扱える |
| `reports/` | forward 計測まとめ | 計測レポート | `baibai-loop-ledger` の output を ad-hoc に dated まとめとして残し、outcome を evidence / macro context fit / sizing / execution / playbook に帰属できる |

成果物（名詞）に対し、それらを生成・計測する **L2 の機械処理（動詞）** は 機械スクリーニング（`screening`）・リサーチ候補選定（`select`）・フォワード計測（`forward backtest`）と呼ぶ。

## 6. 意図的に未自動化のまま残しているもの

完璧を求めず、運用で見えたボトルネックから改善するため、以下は **意図的に手動 + AI 下書き** で運用する。

- **`records/01-macro-context/` の作成は screening 前の必要時更新**: 定期生成や網羅蓄積を目的化せず、判断前提が stale / scope mismatch / premise break の場合だけ更新する
- **Valuation 指標の算出粒度は暫定**: 東証 33 業種、中央値下限 n=10 などの初期値で運用。retro で調整する
- **Rerating Book（1〜6 か月保有）は対象外**: 主戦略は 5-40 営業日の swing に絞る。ただし、短期 thesis が外れた場合に長期保有へ切り替えられる balance sheet / cash flow の耐久性は、portfolio policy の selection principle として確認する。これは固定年数の holding strategy ではなく、売却までの期間が想定より長引いても事業継続性と回収余地が残る銘柄を優先する方針である
- **Playbook 改訂ルールは緩め**: サンプル数 10 件未満なら playbook 据え置きを許容する。厳密な改訂トリガーは運用後に定める

## 7. 参考

- [`architecture/system-overview.md`](./architecture/system-overview.md): 構造・schema・procedure
- [`design-principles.md`](./design-principles.md): 設計原則
- [`operations/README.md`](./operations/README.md): 日々の運用ワークフロー
- [`reference/data-sources.md`](./reference/data-sources.md): データソース
