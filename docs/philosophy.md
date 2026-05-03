# philosophy

Baibai-Loop の **思想・ベースの考え方・進化の歴史** を記述する正本。  
構造・schema・procedure は [`architecture-v1.md`](./architecture-v1.md) に分離する。

## 0. このドキュメントの役割と境界

- **philosophy.md (本ファイル)**: **what we believe + why we chose this design**。価値観、選択の根拠、却下した対立案
- **architecture-v1.md**: **structure + schema + procedure**。具体的 structure、schema、path、front matter 定義、workflow

重なる topic（例: 「なぜ 4 成分か」）は本ファイルで思想として書き、architecture-v1.md は「4 成分である」と事実として受けて構造の記述に進む。

本ファイルは **変更頻度が低い**。運用で大きな信念が揺らいだときだけ更新する。日々の構造変更は architecture-v1.md 側で処理する。

## 1. Baibai-Loop の目的

Baibai-Loop は、日本株スイングトレードの精度を売買反復で上げるための **意思決定ループ基盤** である。

- 核心は「一時的に過剰に売られている割安銘柄を底値で掴んで exit する」
- 名称 "Baibai" = 売買、"Loop" = 反復学習サイクル
- 単発の売買記録ではなく、**売買を含む運用改善ループ** の基盤として設計する

## 2. ベースの考え方（4 つの柱）

各柱は **(a) 信念 / (b) そう信じる根拠 / (c) 却下した対立案** の 3 段構成で記述する。

### 柱 1: 事実と分析の分離

#### (a) 信念

記録されるべき **事実**（`brief`, `candidates`）と、人間/AI の **解釈**（`outlook`, `research`）は **物理的に別ファイル** として管理する。同一ファイルに混在させない。

#### (b) そう信じる根拠

- **コンテキスト汚染の回避**: AI エージェントが過去の journal を参照するとき、事実と意見が混在していると、AI が過去の解釈を「事実」として再生産する可能性がある。ファイル単位の分離で、「解釈ファイルを AI に見せない」選択ができる
- **後知恵バイアスの抑制**: 過去の事実ファイルを見返すとき、当時の解釈と事実を混同しない。解釈は retrospective に見直せる
- **帰責性**: retro で「事実誤認か解釈誤りか」の原因分析が分離できる
- **汚染の不可逆性**: 一度事実ファイルに解釈を混ぜると、どこまでが元の事実か判別困難になる。混ぜないのが最も安全

#### (c) 却下した対立案

- **タグで分ける**: front matter やインラインタグで「ここは事実、ここは解釈」とマークする案。解釈混入時にタグ漏れしやすく、チェックが機械化しにくい
- **front matter で `type: fact/analysis` を切り分け**: 同ファイル内で混在する構造がそもそも汚染を許す。ファイル単位で分けないと「解釈ファイルを AI に見せない」運用が成立しない
- **1 ファイル内で章節を分ける**: 事実節と解釈節の間に書き換えが混入するリスク。特に AI 支援ではファイル単位で touch される前提で考えるとリスクが高い

### 柱 2: マクロ優位 (76/24)

#### (a) 信念

トレード判断は **マクロ × ミクロ** の組み合わせだが、比重は **76% : 24%** とする。長期トレンド（マクロ）がなければ割安検出（ミクロ）は機能しない。この比率は運用途中で動かさない。

#### (b) そう信じる根拠

- **逆風下の割安は構造的 trap**: マクロ逆風業種の個別銘柄が割安でも、構造的に売られ続ける（valuation trap）。短期のリバウンドはあっても中期の戻りが期待できない
- **過去の #5 / #6 で macro を修飾因子扱いした失敗経験の想定**: 個別割安の検出精度だけを磨いても、マクロ逆風下では勝率が出ないと想定される（v1 運用前だが、先行設計からの学び）
- **Top-down の合理性**: 世界情勢 → 日本経済 → 日本株 の階層で判断することは、マクロショックの transmission パスと一致している
- **底値狙いの性質**: 「一時的に過剰に売られている」の「過剰」を判定するには、マクロトレンドが追い風であることの確認が前提

#### (c) 却下した対立案

- **50/50**: macro と micro の重みが同等だと、macro 逆風下の micro 割安を拾う危険が残る。比重で優位を明示しないと運用時に流される
- **90/10**: macro 一辺倒。個別銘柄の変動情報を活用しきれない。短期スイングにはマクロ 100% は粗すぎる
- **動的 weight（機械学習による調整）**: candidate 数が 3 桁に満たない 1 人運用では、weight 学習の統計的根拠が出ない。学習データ不足で over-fit するリスク
- **比率を明示せず「総合判断」とする**: 運用時に「今回は macro より micro を重視」の判断ブレが起きる。数値で固定するほうが規律が保てる

### 柱 3: Feedback loop 先行

#### (a) 信念

基盤設計が完成してから運用を開始するのではなく、**不完全でも loop を 1 周回してから改善** する。設計完成と運用開始の間に遅延を作らない。

#### (b) そう信じる根拠

- **#5 の失敗**: Phase 6（学習ループ設計）が最後で、feedback が始まるまでの距離が遠かった。基盤が完成した頃には、実運用との乖離が大きくなる
- **運用による設計検証**: 実際に loop を回すと、不要と判明する設計要素が出てくる。先行精緻化すると YAGNI な設計に時間を投じることになる
- **MVP-first の精神**: 最小限で動くものを出し、運用のフィードバックで改善する、というソフトウェア開発の原則と整合
- **Retro の意味**: loop が回っていないと retro の材料がない。材料がないと改善の方向が見えない

#### (c) 却下した対立案

- **完成設計 → 運用開始**: #5 の方式。設計に時間をかけすぎて、実運用に届くまでに構造が陳腐化するリスク
- **Event Store / Feature Store の先行導入**: #5 案。1 人運用で YAGNI、schema migration コストが運用開始を遅らせる
- **設計凍結なしに実運用**: 構造なしに loop を回すと、何が記録されるべきか不定で混乱する。**最低限の構造**（4 成分 + front matter）で開始し、内容は育てる

### 柱 4: Markdown 駆動

#### (a) 信念

DB / Feature Store を先行導入しない。**front matter が揃った markdown** を事実・分析の共通基盤とする。script で後付け抽出可能な設計にしておく。

#### (b) そう信じる根拠

- **1 人運用の実行性**: DB 運用は個人で維持しきれない。markdown は git で完結
- **Git との親和性**: diff / blame / history が標準ツールで扱える。変更の追跡が自然にできる
- **AI 支援との親和性**: AI 下書き + 人間最終確認の協働が自然。AI にとっても markdown は read/write しやすい
- **後付け抽出の可能性**: front matter（YAML）が揃っていれば、将来 script で集計・分析できる。構造化と自由度の balance 点
- **vendor lock-in の回避**: Notion / Airtable 等は移行困難。markdown + git は永続的

#### (c) 却下した対立案

- **SQLite 早期導入**: schema migration コストで運用開始が遅れる。1 人で schema を進化させながら運用するのは負担大
- **JSON / YAML ファイル（markdown なし）**: 人間の readability が低い。運用メモや解釈の記述に向かない
- **Notion / Airtable 等外部ツール**: vendor lock-in、git 統合困難、料金、AI 支援時の access 手続きなど運用課題が多い
- **最初から RDB + ORM**: 1 人運用の YAGNI 極致。運用で必要になるまで導入しない

## 3. なぜ 4 成分か（3 層ではなく斜交 2×2）

Baibai-Loop は事実層と分析層を **マクロ/ミクロ で斜交配置** した 4 成分で構成する。階層的 3 層モデルではない。

```
              マクロ軸                 ミクロ軸
事実軸   (a) brief               (b) candidates
分析軸   (c) outlook                (d) research
```

- **a↔c ペア（マクロ）**: brief（事実）が積み上がって outlook（見解）になる
- **b↔d ペア（ミクロ）**: candidates（事実）からの選定で research（分析）が作られる
- **a↔b ペア（事実層）**: マクロとミクロの事実は並行して蓄積される
- **c↔d ペア（分析層）**: outlook × research の統合が売買判断を生む

### なぜ 3 層モデルではないか

階層的 3 層（事実 → 解釈 → 判断）だと、マクロとミクロが同じ層で混ざり、責務が重なる。事実と分析は独立した 2 本のトラック（マクロ事実 → マクロ見解 / ミクロ事実 → ミクロ分析）として存在し、**統合は research の中で起こる**。斜交配置のほうが責務境界が自然になる。

## 4. なぜ 2 トラック（macro 独立 + micro 売買ループ）か

### (a) Macro track（独立）

`brief → outlook` は **売買イベントと独立に更新される**。CPI / BOJ / FOMC などのマクロイベントは売買の有無に関わらず発生し、記録される必要がある。

### (b) Micro track（売買ループ）

`candidates → research → trades → reviews` は **売買判断と連動する** ループ。screening 実行 → 選定 → 深掘り → 採用 → 執行 → 検証 → retro feedback。

### (c) 2 トラックの統合点: research

2 トラックは **research で統合** される。research は:

- **入力**: 最新 candidates（ミクロ事実）+ 最新 outlook（マクロ見解）
- **出力**: 個別銘柄の深掘り packet + 採用判定

outlook がなければ research が作れない（Bootstrap 規則）。これは、マクロ見解なしに個別銘柄を評価しないという柱 2（マクロ優位 76/24）の帰結である。

## 5. 用語選定の思想

v1 で確定した 4 成分の名前は、**役割を一語で表す** ことと **投資業界の慣習** を両立する。

| 成分 | 名前 | 採用理由 | 却下案 |
| --- | --- | --- | --- |
| a | `brief` | 「short fact+points doc」の業界標準語。journal（時系列ログ）より役割に忠実 | journal（log 含意が強い）、record、ledger |
| b | `candidates` | 機械的ふるいで残った銘柄群というデータの実体を直接表す。フェーズ名 (brief / outlook / research) と粒度が揃う | screened（動詞由来で粒度不一致）、screening（プロセス感）、filtered |
| c | `outlook` | humble、更新しやすい。"strategy" は大げさ、"thesis" は academic | strategy（大げさ）、thesis（重い）、outlook（見通し限定）、perspective |
| d | `research` | 業界標準、「仮説を立てて検証する」ワークフローと整合 | deep-dive（2 語）、investigation（堅い）、analysis（generic）、memo（軽い） |

### ルート直下配置の意図

- ワークフローを単純化
- 他の直下 dir（`docs/`, `records/_playbooks/`）と同格に扱う
- path の浅さで成分の「使用頻度」と「重要度」を表現

## 6. v1 の時点で意図的に残す未熟さ

完璧を求めず、v1 運用で見えたボトルネックから改善するため、以下は **意図的に未完成** のまま v1 運用を開始する。

### 未熟さ 1: `records/02-outlook/` は brief からの手動集約

将来 `analysis/` 集約層で自動化する想定だが、v1 では人間 + AI による手動集約。v1 運用の手間を計測してから自動化仕様を決める。

### 未熟さ 2: `records/03-candidates/` は機械的ふるいを手動 + AI で実行

将来 script 化を検討するが、v1 では手動 + AI 下書き。閾値やデータソースを運用で validate してから自動化する。

### 未熟さ 3: Valuation 指標の算出粒度は暫定

東証 33 業種を初期値、中央値下限 n=10 など暫定値。retro で調整する。

### 未熟さ 4: Rerating Book（1〜6 か月保有）は未実装

v1 は Swing only（2 か月以内）に絞る。Rerating は v2 以降の検討。

### 未熟さ 5: Playbook 改訂ルールは緩め

サンプル数 10 件未満なら playbook v1 据え置きを許容（#7 から継承）。厳密な改訂トリガーは v1 運用後に定める。

これらは **MVP-first の原則通り**、1 周目で見えたボトルネックから改善する。

## 7. 進化の歴史

### 7.1 系譜（issue 単位）

| issue | 主要構造 | playbook 数 | score | マクロ比重 | 状態 |
| --- | --- | --- | --- | --- | --- |
| #5 (design v1.1) | 2 ブック + 5 playbook + Event/Feature Store + Universal score | 5 | 1 本総合 | 修飾因子 | 却下 |
| #6 (MVP-first) | Swing only + markdown 駆動 + 2 playbook | 2 (event-driven) | 廃止 | 修飾因子 | 継承中 |
| #7 (valuation v2 + 4 成分) | valuation v2 + 4 成分 (a/b/c/d) + Macro gate 格上げ | 2 (valuation-based) | 4 軸評価（総合点なし） | 76/24 hard gate | 継承中 |
| **v1 (本アーキテクチャ)** | **4 成分 + 下流**、用語確定、philosophy 明文化 | 2（#7 継承） | 4 軸評価（#7 継承） | 76/24（#7 継承） | **唯一の前進ライン** |

### 7.2 主要な転換点

- **#5 → #6**: MVP-first へ。抽象過剰を切り、feedback loop 先行に
- **#6 → #7**: valuation-based 割安検出を核に、マクロ 76/24 を hard gate 化、4 成分アーキ採用
- **#7 → v1**: 用語確定（`records/01-brief/`, `records/03-candidates/`, `records/02-outlook/`, `records/04-research/`）、screening をサブシステム化、philosophy 明文化、migration 実施

### 7.3 なぜ v1 で philosophy を明文化したか

用語が確定したタイミングで、**what we believe + why** を残さないと、将来の編集者（人間 or AI）が意思決定の根拠を失う。「なぜ 4 成分なのか」「なぜ 76/24 なのか」を理由とともに残す。

「ベースの考え方」は判断の土台（= why）であり、コンテナ（= what）ではない。**what を書けば philosophy が書けるわけではない**。却下した対立案とその理由があって初めて philosophy になる。この原則を本ファイルに守らせるため、4 つの柱は (a) 信念 / (b) 根拠 / (c) 却下案 の 3 段構成を必須とする。

## 8. 参考

- [`architecture-v1.md`](./architecture-v1.md): 構造・schema・procedure
- [`design-principles.md`](./design-principles.md): 設計原則
- [`workflow.md`](./workflow.md): 日々の運用ワークフロー
- [`data-sources.md`](./data-sources.md): データソース
- 過去 issue: [#5](https://github.com/koumatsumoto/baibai-loop/issues/5), [#6](https://github.com/koumatsumoto/baibai-loop/issues/6), [#7](https://github.com/koumatsumoto/baibai-loop/issues/7)
