# philosophy

Baibai-Loop が **何を信じ、なぜこの設計を選んだか** の正本。価値観・選択の根拠・却下した対立案を残す。
構造・schema・procedure は [`architecture/system-overview.md`](./architecture/system-overview.md) と [`architecture/README.md`](./architecture/README.md) に分離する。

このファイルは変更頻度が低い。運用で大きな信念が揺らいだときだけ更新する。日々の構造変更は [`architecture/`](./architecture/) 側で処理し、投資判断ドメインモデルは [`concepts.md`](./concepts.md) を正本とする。

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

### 柱 2: Macro regime discipline

#### (a) 信念

トレード判断では、security-level の割安さだけでなく、macro / sector regime を必ず gate として扱う。`macro 76% / security-level 24%` は、判断時に割く attention / review time / cognitive budget の policy weight を表す説明補助であり、採用可否や position size を直接計算する比率ではない。

Validator-visible な採用可否と sizing cap は、macro regime gate と portfolio policy が担う。

#### (b) そう信じる根拠

- **逆風下の割安は構造的 trap**: マクロ逆風業種の個別銘柄が割安でも、構造的に売られ続ける（valuation trap）。短期のリバウンドはあっても中期の戻りが期待できない
- **Top-down の合理性**: 世界情勢 → 日本経済 → 日本株 の階層で判断することは、マクロショックの transmission パスと一致している
- **底値狙いの性質**: 「一時的に過剰に売られている」の「過剰」を判定するには、マクロトレンドが追い風であることの確認が前提
- **実装可能性**: 注意配分は思想として固定し、実際の gate / cap は outlook、portfolio policy、research の構造 field で検査するほうが再現性が高い

#### (c) 却下した対立案

- **Macro を prose の注意喚起だけにする**: macro 逆風下の割安を拾う危険が残る。Macro regime gate と portfolio policy の cap に落とす必要がある
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

DB / Feature Store を先行導入しない。**front matter が揃った Markdown と YAML** を事実・分析の共通基盤とする。script で後付け抽出可能な設計にしておく。

#### (b) そう信じる根拠

- **1 人運用の実行性**: DB 運用は個人で維持しきれない。ファイル + git で完結
- **Git との親和性**: diff / blame / history が標準ツールで扱える
- **AI 支援との親和性**: AI 下書き + 人間最終確認の協働が自然
- **後付け抽出の可能性**: front matter（YAML）が揃っていれば、script で集計・分析できる
- **vendor lock-in の回避**: Notion / Airtable 等は移行困難。Markdown + git は永続的

#### (c) 却下した対立案

- **SQLite 早期導入**: schema migration コストで運用開始が遅れる。1 人で schema を進化させながら運用するのは負担大
- **JSON / YAML ファイル単独（Markdown なし）**: 人間の readability が低い。運用メモや解釈の記述に向かない
- **Notion / Airtable 等外部ツール**: vendor lock-in、git 統合困難、料金、AI 支援時の access 手続きなど運用課題が多い
- **最初から RDB + ORM**: 1 人運用の YAGNI 極致。運用で必要になるまで導入しない

## 3. なぜ lifecycle loop か（3 層の階層モデルではない）

Baibai-Loop は、単純な「事実 → 解釈 → 判断」の 3 層モデルではなく、portfolio policy から review attribution までの lifecycle loop として扱う。

```text
portfolio policy
  -> brief
  -> outlook
  -> candidates
  -> research
  -> trades
  -> reviews
  -> playbooks
  -> candidates
```

- **policy**: 目的、制約、資本、許容リスク、time horizon を固定する
- **brief / candidates**: fact layer。Macro / market observations と security-level screen output を分ける
- **outlook / research**: analysis layer。Macro regime view と investment memo を分ける
- **trades**: execution record。order / entry した判断がどう約定・保有・決済されたかを記録する
- **reviews / playbooks**: outcome attribution を playbook feedback に戻す

階層的 3 層（事実 → 解釈 → 判断）だけだと、macro regime、security-level thesis、execution、review attribution が同じ「判断」層に混ざり、責務が重なる。Lifecycle loop として分けるほうが、どこで候補を拾い、どこで落とし、どこで改善するかを追いやすい。

## 4. なぜ 2 トラック（macro 独立 + security-level 売買ループ）か

### (a) Macro track（独立）

`brief → outlook` は **売買イベントと独立に更新される**。CPI / BOJ / FOMC などのマクロイベントは売買の有無に関わらず発生し、記録される必要がある。

### (b) Security-level track（売買ループ）

`candidates → research → trades → reviews` は **売買判断と連動する** ループ。screening 実行 → 選定 → 深掘り → 採用 → 執行 → 検証 → retro feedback。

### (c) 2 トラックの統合点: research

research は:

- **入力**: 最新 candidates（security-level screen output）+ 最新 outlook（macro regime view）+ portfolio policy
- **出力**: investment memo と採用可否、position sizing、execution への接続

outlook がなければ research が作れない。これは、macro regime なしに個別銘柄を評価しないという柱 2 の帰結である。

## 5. 用語選定の思想

主要 artifact の名前は、**役割を一語で表す** ことと **投資業界の慣習** を両立する。

| Artifact | 名前 | 採用理由 | 却下案 |
| --- | --- | --- | --- |
| `brief` | brief | 「short fact+points doc」の業界標準語。journal（時系列ログ）より役割に忠実 | journal（log 含意が強い）、record、ledger |
| `candidates` | candidates | 機械的ふるいで残った銘柄群というデータの実体を直接表す | screened（動詞由来で粒度不一致）、screening（プロセス感）、filtered |
| `outlook` | outlook | humble、更新しやすい。1-6m の regime view として役割に忠実 | thesis（重い）、perspective |
| `research` | investment memo | 業界標準の memo 形式に寄せつつ、repository path としては research を維持できる | deep-dive（2 語）、investigation（堅い）、analysis（generic） |
| `trades` | execution record | trade / order / fill / cancellation を execution layer として扱える | entry log（entry に偏る）、order log（約定後の position を扱いにくい） |
| `reviews` | attribution review | outcome を evidence、macro regime gate、sizing、execution、playbook に帰属できる | retro only（事後集計に偏る）、postmortem（失敗だけに見える） |

## 6. 意図的に未自動化のまま残しているもの

完璧を求めず、運用で見えたボトルネックから改善するため、以下は **意図的に手動 + AI 下書き** で運用する。

- **`records/03-outlook/` の集約は手動 + AI**: 自動集約は将来の検討対象。運用負荷を計測してから自動化仕様を決める
- **Valuation 指標の算出粒度は暫定**: 東証 33 業種、中央値下限 n=10 などの初期値で運用。retro で調整する
- **Rerating Book（1〜6 か月保有）は対象外**: 主戦略は 5-40 営業日の swing に絞る。ただし、短期 thesis が外れた場合に長期保有へ切り替えられる balance sheet / cash flow の耐久性は、portfolio policy の selection principle として確認する
- **Playbook 改訂ルールは緩め**: サンプル数 10 件未満なら playbook 据え置きを許容する。厳密な改訂トリガーは運用後に定める

## 7. 参考

- [`architecture/system-overview.md`](./architecture/system-overview.md): 構造・schema・procedure
- [`design-principles.md`](./design-principles.md): 設計原則
- [`operations/README.md`](./operations/README.md): 日々の運用ワークフロー
- [`reference/data-sources.md`](./reference/data-sources.md): データソース
