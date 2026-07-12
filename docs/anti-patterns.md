---
title: "Anti-patterns"
summary: "投資判断、data、schema、validator、AI運用で繰り返し防ぐ失敗パターンとcommit前checklist。"
doc_type: governance
status: active
last_reviewed: 2026-07-12
---

# anti-patterns

baibai-loop での AI agent 作業で観測された失敗パターン集と、再発防止のためのチェックリスト。
PR で繰り返し指摘される類型は本ドキュメントに集約し、self-review の strict gate として運用する。

このドキュメントは事後分析のためではなく **作業前 / commit 前 / PR 前のチェックリスト** として
読まれることを意図する。新規 macro context / research を書く前に必ず該当節を読み返すこと。

## 0. 全 anti-pattern 共通の根本原因

PR #68 (2026-05-04 旧 outlook + 6590 research) で 2 ラウンドのレビューで合計 21 件の指摘を
受けた。共通する根本原因は以下:

1. **一次情報を確認せずに二次情報・推測で書く**
2. **数値を機械的に検算しないまま記述する**
3. **データの「異常さ」に対して原因 cross-check を skip する**
4. **schema / 実装の意味を読まずに「だろう」で書く**
5. **fact 層と分析層の境界を曖昧にする**
6. **依存関係 (macro context → research) の整合性を意識しない**
7. **公表日 / source の最新性 / source の粒度を確認しない**
8. **schema validator の抜け道を意識しない**

これらは「**作業を雑に進めた結果**」であり、コミット前に該当 anti-pattern checklist を 1 周
すれば全件防げた性質のもの。**速く書くことより正しく書くことを優先する**のが本リポジトリの
基本方針 (詳しくは [`doctrine.md`](./doctrine.md))。

## 1. AP-01: 一次情報を直接確認せず二次情報・推測で書く

### 観測された症状
- 122 条関税を「13% 上乗せ」と書いた (Federal Register 一次情報は 10% ad valorem)。
  trade-weighted estimate の二次情報を引用元なしに断定した
- TSMC 「Capex $52-56B レンジ」「先端プロセス 70-80% 配分」「2026 年売上 +30%」を
  Q1 release から確認したと書いた (実際は Q4 transcript / IR archive 由来)
- NVIDIA 「Sovereign AI 売上 300 億 USD」「Q3-Q4 commitment 倍増」を press release 由来と
  して書いた (実際は press release では未記載、要 transcript / 10-K)
- 6590 芝浦メカトロニクス の顧客を TSMC / Samsung / Kioxia と断定した (公式製品ページで
  確認できるのは製品領域までで、顧客別売上比率は有報未確認)
- OPEC+ 5/3 statement を旧 outlook / brief 作成日 (5/4) に確認していなかった

### 根本原因
- 自分の事前知識ベースで「だろう」と書く habit
- 二次情報・分析記事で見た数字を一次情報の数字と区別せず引用する
- 一次情報 URL の本文を読まず source ID だけ書く

### 再発防止チェックリスト (commit 前必須)

- [ ] **すべての数値・固有名詞 (社名・組織名・地名・政策名) について、引用元 URL を文書内に明示しているか**
- [ ] その URL を実際に WebFetch / curl で取得し、本文に記載があることを確認したか
- [ ] **source の policy / rate / date / scenario が本文主張と一致しているか** (URL を貼っただけで終わらせない)
  - 例: 「Section 122 trade-weighted 13%」と書く場合、貼った Global Trade Alert source の中で
        13.0% は **15% シナリオ** の数値であり、10% 法定 (Proclamation 11012) 前提と整合しない。
        10% 前提なら 11.4-11.5%、15% シナリオを使うなら法定が 15% の場合の話だと明記する
  - 例: 「BEA 公表」と書く場合、その URL が press release / FRED / BEA Schedule のどれか、
        対象月 (March 2026 vs April 2026) が一致するか、speech だけで release ではないか
- [ ] 「業界レポート」「アナリスト試算」「外部分析の trade-weighted estimate」などの二次値は、
      一次値と明確に区別して `(外部 estimate, source: ...)` の形で書いているか
- [ ] 銘柄固有の事業構造 (顧客 / 地域 / 親会社取引比率) を断定する場合、有価証券報告書 / 決算
      説明資料 / 統合報告書のいずれかに直接 URL でリンクしているか。リンクなしの断定は禁止
- [ ] macro context の発行日付近に大型 statement (FOMC / BOJ / OPEC+ / CPI / PCE) が予定
      されていれば、発行前に「最新版が出ていないか」を schedule で確認したか

## 2. AP-02: 数値計算を機械的に検算しない

### 観測された症状
- `adv_participation_pct = 0.005 / 85.4 * 100 = 0.00585%` を **0.585** と記述 (100 倍ズレ)
- 利確 target を「PER 7.35 → 16 への正常化 = entry 価格 +20-30%」と記述。実際は EPS 一定なら
  +118%、+20-30% を狙うなら PER target は 8.8-9.6
- 為替変動率を %、bp を混同するリスク

### 根本原因
- 数式を頭の中だけで処理して紙 / 電卓 / Python で再計算しない
- 「だいたい合ってる」感覚で commit する
- 単位 (%/bp、円/USD、千 / 百万 / 億) の整合性を check しない

### 再発防止チェックリスト

- [ ] **各数値計算について、Python / 電卓で 1 回検算した結果を文書内のコメントまたは
      `(計算: A / B * 100 = C)` の形で残しているか**
- [ ] execution policyの最大許容価格を、5年base terminal price + 累積配当と明示した要求CAGRから再計算し、合法tickへ切り下げたか。終値からの任意率やclaimed max priceを転記していないか
- [ ] 単位を明示しているか (% / bp / pt / 倍 / 円 / USD)
- [ ] portfolio outcomeでは、contribution / withdrawalだけをexternal flowとしてTWR分母へ入れ、buy/sell・reservation・配当・費用・確定税を二重にflow扱いしていないか
- [ ] 価格 → リターン換算は (新値 - 旧値) / 旧値 * 100 で計算しているか
- [ ] PER / EV/EBITDA 等の倍率変化は EPS / EBITDA 一定なら株価リターン = (target / current - 1) * 100
- [ ] 桁数 (0.005 vs 0.05 vs 0.5、1e-3 vs 1e-2 vs 1e-1) を音読で確認したか

## 3. AP-03: データの「異常さ」に対して原因 cross-check を skip する

### 観測された症状
- 6590 芝浦メカトロニクス の `price_change_60d: -0.8129` (-81%) を「過剰売り」と解釈し、
  株式分割 (2026-03-01 効力 1:5) の split artifact 可能性を確認しなかった

### 根本原因
- 株価が極端に動いた (>= ±50%) のに「需給」「業績」「セクター回転」のいずれかで説明できる
  と決めつけ、corporate action (split / 合併 / TOB / 上場区分変更) の可能性を忘れる
- candidates パイプラインが split 調整しているか、`record_date` ベースか `effective_date`
  ベースかを確認しない

### 再発防止チェックリスト

- [ ] candidates 由来の `price_change_60d` / `price_change_20d` が **±50% を超える銘柄**は、
      research に進める前に以下を確認:
  - [ ] EDINET の臨時報告書・有価証券届出書で 60 日 / 20 営業日 期間内の corporate action
        (株式分割 / 併合 / 合併 / TOB / 第三者割当) を確認
  - [ ] TDnet / 適時開示で同期間の重要発表を確認
  - [ ] J-Quants の adjustment_factor が分割を反映しているか実装で確認
- [ ] `self_range_percentile` が下位 5% 以下の銘柄も同様に corporate action を必ず確認
- [ ] portfolio outcomeで保有期間の`adjustment_factor != 1`を検出したとき、adjusted closeや0円補完で継続せず`corporate_action_unresolved`にしたか
- [ ] calibration forward で stale / missing exit を resolved return に混ぜず、delisting unknown として明示したか
- [ ] adjustment factor の観測可否と corporate-action event coverage を同一視していないか
- [ ] `forward PER` と `trailing PER` の乖離が ±100% を超える場合、決算特殊要因 (税引前
      一過性 gains / losses、減損、グループ再編) の可能性を有報で確認

## 4. AP-04: schema / 実装の意味を読まずに推測で解釈する

### 観測された症状
- candidates の `sector_relative_strength_percentile: 1.0` を「同業種内で最も強い銘柄」と
  解釈。実装は `_rank_to_percentiles` で sector level の rank (electronics sector が全 33
  業種中で強い) を返す。個別銘柄の同業種内相対強度ではない
- candidates / macro context YAML schema の追加プロパティ可否を確認せず `note` / `previous_change`
  を勝手に追加 → validate error
- 旧 outlook YAML schema の `source_refs` が brief YAML パスに限定されることを確認せず
  research-log.md を指定 → validate error

### 根本原因
- field 名から意味を「だろう」で推測する
- schema JSON / 実装コードを読み直さない
- 既存サンプルとの diff を意識しない

### 再発防止チェックリスト

- [ ] candidates / macro context / research の field を新規に解釈・記述する前に、対応する
      JSON schema (`records/_schemas/*.json`) を読み返したか
- [ ] 計算系 field (percentile / rank / change / hit) は src 実装 (`src/baibai_loop/screening/`)
      で計算ロジックを確認したか
- [ ] 既存ファイル (candidates、macro context、research) のサンプル形式に
      従っているか、独自構造を勝手に追加していないか
- [ ] `additionalProperties: false` の object に独自 key を追加していないか
- [ ] holding review / portfolio outcomeがledger・decision packet・benchmark observationのref/hashを検証し、scalarやsource hashのdriftを通していないか

## 5. AP-05: fact 層と分析層の境界を曖昧にする

### 観測された症状
- 旧 brief の `note` / `fact_memos` / `events` に「FOMC タカ派ホールドの正当化材料」「需要側
  冷却の early evidence hit」「油価高値圏粘着の構造要因」「122 条効果が顕在化」などの解釈・因果
  推論・意味付け表現を書いた (doctrine.md#fact-analysis-separation で禁止)

### 根本原因
- 旧 brief = 事実層 / outlook = 分析層 の境界を意識せず、便利な要約として書く
- doctrine.md#fact-analysis-separation の禁止表現リスト (「示唆」「背景」「受けて」「意味する」) を
  読み返さない

### 再発防止チェックリスト

- [ ] candidates などの事実層に以下のような解釈表現が含まれていないか:
  - [ ] 「示唆する」「観測される」「受けて」「背景に」「意味する」
  - [ ] 「正当化材料」「early evidence hit」「顕在化」「構造要因」
  - [ ] 「注目すべき」「重要な」「焦点となる」 (Major/Notable は閾値ラベルでありこの意味では
        使わない)
- [ ] candidates などの事実層に解釈・因果推論・予測を混ぜていないか
- [ ] 解釈・因果推論・予測は macro context / research の分析層に移したか
- [ ] macro context で使った外部記事・統計は source metadata として残し、記事本文や網羅的 fact を repo に蓄積していないか

## 6. AP-06: macro material delta と個別判断の境界を曖昧にする

### 観測された症状
- future の macro context を判断時点の情報として使った
- stale / missing macro context を理由に、決定論的なscreeningまたは候補比較を停止した
- macroのmaterial deltaを銘柄別の事実や機械rankingへ混入した
- material deltaが個別5年期待値へ影響するのに、decision packetの根拠・反証へ接続しなかった

### 根本原因
- macro contextを候補選別用のsector/ranking入力だと誤解する
- stale warningとfuture errorを区別しない
- doctrine.md の柱（事実層と分析層の物理分離、macroは判断の補助）を運用で守らない

### 再発防止チェックリスト

- [ ] macro contextを使う場合、`as_of`が判断時点より未来ではないか（futureは停止、staleはwarning）
- [ ] `inputs`のinput_id、`material_deltas` / `sizing_cautions` のsource_ids、statusを照合したか
- [ ] macro summaryをcandidateのfact、E[r]順位、機械sizingへ混入していないか
- [ ] material deltaが個別仮説に影響する場合だけ、decision packetの判断と反証にsource付きで接続したか
- [ ] **機械化チェック**: macro context 編集後に `uv run baibai-loop-validation` を実行したか

## 7. AP-07: 公表日 / 期間 / source の最新性確認を skip する

### 観測された症状
- 米 4 月 PCE 公表予定を「5/30 前後」と書いた (BEA schedule で確認した正確な日付は
  2026-05-28 8:30 EDT)
- 旧 outlook 5/4 公開時に OPEC+ 5/3 statement を反映しなかった
- next_events に source_ids を紐付けず、BLS schedule などの一次情報を素通り

### 根本原因
- 「だいたいの日付」感覚で next_events に書いてしまう
- 発行直前の重要 release (前日・当日) を「まだ早すぎる」と勝手に判断して skip
- source metadata と確認対象 release の紐付けを必須運用していない

### 再発防止チェックリスト

- [ ] macro context / research 内の **すべての日付** について、source の publish schedule か
      release date を WebFetch で再確認したか
- [ ] macro context 発行日 ± 5 営業日に予定された FOMC / BOJ / CPI / PCE / NFP / OPEC+ のいずれかが
      あれば、最新 release / statement / minutes が出ているかを必ず確認
- [ ] macro context の `inputs.articles[]` / `inputs.indicator_series[]` に、判断へ使った外部記事・指標 series と
      `used_for` を残したか
- [ ] 次に更新すべき大型 event は `refresh_triggers[]` に具体的に残したか
- [ ] 「随時」「○月下旬」「前後」のような曖昧表現を避け、確認できた具体日付を書く
- [ ] historical calibration panel が cohort as-of 以下の master snapshot を読み、latest snapshot へ fallback していないか

## 8. AP-08: schema validator の抜け道を意識しない

### 観測された症状
- `adv_participation_pct: 0.585` (100 倍ズレ) を validator が catch しなかった
- 当初の整合チェックを `avg_turnover_oku` 不在時には silently skip するように実装、
  required field 化を忘れた → 抜け道残存
- schema 管理している nested object が未知 field を許しており、current contract 以外の値を取り込めた
- `judgment.recommendation: reject` のpacketに買い注文が紐づき、非採用判断と矛盾していた
- `except TypeError, ValueError:` のような Python 2 風に見える except をめぐって、レビューで
  「構文エラー」なのか「Python 3.14 の PEP 758 による複数例外捕捉」なのかが混乱した。
  本 repo では可読性とレビュー容易性を優先し、複数例外捕捉は `except (A, B):` に統一する

### 根本原因
- validator を「データが揃っている前提」で実装し、欠損時の挙動を「skip」にする
- corner case (rejected / deferred / 0 値 / null) のテストを書かない
- unresolved cohort を aggregate から silent drop し、coverage が完全であるかのように扱う
- ユーザ指摘で初めて抜け道に気付く
- runtime / formatter target の違いを確認せず、構文レビューと formatter 挙動を推測で判断する

### 再発防止チェックリスト

- [ ] validator rule を追加・修正する場合、以下の corner case の test を必ず書く:
  - [ ] decision packetは7永久損失軸、source/as-of、3年/5年bear/base/bullを欠くと`incomplete`になる
  - [ ] decision packetは`input_snapshot`、判断時`market_price`、valuation factを欠くと`incomplete`になる
  - [ ] snapshot sourceのticker不一致、未来as-of/retrieval、未知source ID、不正unit/typeを拒否する
  - [ ] source retrievalとmarket price observationがAI proposal時刻より後なら拒否する
  - [ ] canonical decision filenameの日付・tickerがsnapshot identityと一致する
  - [ ] AI value captureはsourceを持ち、`not_material`ならrole/decision weightを持たず、`disrupted`ならstructural_decline riskと根拠が接続する
  - [ ] `entry_price_basis: observed_market_price`はsnapshotの判断時priceと一致する
  - [ ] execution policyはstale / historical / synthetic quote、max price超過、cash / dry-powder不足を`defer`にし、全orderがboard lot・合法tick・max priceを守る
  - [ ] local candidate YAML / SQLite pathをtracked decisionの参照先にせず、provider・dataset・retrieved_atをsnapshotへ固定する
  - [ ] scenarioの利益、株数変化、terminal multiple、配当、CAGRを再計算し、配当をterminal priceと二重計上できない
  - [ ] primary evidence不足でhigh confidenceまたは通常sizingのbuyへ進めず、期限付きoverrideと縮小sizingを要求する
  - [ ] buy proposalのindependent reviewは別agent/session・別artifactで作り、packet hash、reviewer run ID、6 scenario再計算、全load-bearing source照合、変更有無へ束縛される
  - [ ] AI proposalは`proposed_at <= reviewed_at`、一次情報不足overrideは別envelopeでhuman decision reference・認識risk axesを持ち、review後かつ期限内に承認される
  - [ ] 関連 field が **不在** の場合 (skip / error どちらが正しいか)
  - [ ] 関連 field が **null** の場合
  - [ ] 関連 field が **0 / 負値** の場合 (decision との整合性)
  - [ ] schema 管理している **nested object** が未知 field を許していないか
  - [ ] **既存 packet** (4/25 research 5 件など) が新 rule で breakage しないか、する場合は
        同 commit で fix する
- [ ] ledger eventを導入・変更する場合、reservationとbuy execution、terminal orderとrelease、cash不足、guard超過、expiry後のbuy、保有超過sellをhard errorとして確認したか
- [ ] concentrationはholding market value + active reservationをledgerの`total_capital_yen`で割り、warning + 期限付きoverrideとして扱うことを確認したか
- [ ] human result CLIを変更する場合、報告なしでno write、proposal/approval URL必須、missing fieldの質問、canonical非上書き、source hash drift拒否をcontract testで確認したか
- [ ] decision packetがapprovedの場合、source snapshot、scenario、independent review、execution inputが同一packet hashに束縛されるか
- [ ] **新 validator rule を追加するときは必ず本 docs/anti-patterns.md AP-08 の
      checklist を更新**して、次回 review で同じ穴が再発しないように記録する
- [ ] 整合チェック (cross-field consistency) は片方の欠損で skip しないよう、依存 field を
      required 化する
- [ ] 複数例外を捕捉する場合は必ず `except (A, B):` と書く。`except A, B:` は禁止。
      commit 前に `rg -n "except [A-Za-z0-9_.]+, [A-Za-z0-9_.]+" src tests` が 0 件であることを確認する
- [ ] **CLI subcommand / selection 機能を削減する場合、以下を同 commit で揃える** (PR #248 で 5 名レビューで指摘):
  - [ ] `src/baibai_loop/screening/cli/app.py` の subparser + `add_argument` 引数 + `main()` の dispatch
  - [ ] `src/baibai_loop/screening/cli/{__init__.py,query.py,cache.py,run.py}` の関数 / import
  - [ ] `src/baibai_loop/screening/cli/common.py` の専用 helper (`_parse_profiles_arg` のような callers が消えた helper)
  - [ ] `docs/` 全 grep (`rg <subcommand> docs/ records/ reports/`): runbook の bash example、reference の CLI 表、components / screening の説明文、`docs/reference/screening-runtime.md` の subcommand 一覧
  - [ ] `.agents/skills/`と`.claude/skills/`全grep: canonical skillとsymlinkが当該CLIを参照していないか
  - [ ] `docs/reference/screening-runtime.md` §3 (env var) / §8 (rules baseline) と `docs/workflow/screening.md` の selection block 節
  - [ ] 関連 test fixture (test_screening_cli の sweep / scorecard テスト等)
- [ ] **screening evidence pattern を削減する場合、以下を同 commit で揃える** (PR #246 で 5 名レビューで指摘):
  - [ ] `records/_config/screening-rules/*.yaml` の `screening_playbooks.<playbook>` と
        `research_selection_playbook_order` から削除
  - [ ] `src/baibai_loop/screening/rules.py` の `match` 句 / PLAYBOOK_* / REASON_* / `_<playbook>_*` 関数
  - [ ] `src/baibai_loop/screening/rule_config.py` の `<Name>Playbook` class と Union 型
        (`screening_playbooks: Mapping[..., A | B | C]`) と `match` 句
  - [ ] `src/baibai_loop/screening/selection/ranking.py` の sort key match arm
  - [ ] 削除根拠は保有 outcome の calibration で示す (安易な削除で有効な割安タイプを失わない)
- [ ] **judgment-gate 系の必須 contract を追加する場合、bypass を test で塞ぐ**:
  - [ ] data 不在 label で hard trigger を回避できないか
  - [ ] label と根拠数値の不整合が catch されるか
  - [ ] `regime` key 欠落のような partial mapping が `required` 違反として catch されるか

## 9. AP-09: 外部AI・broker事実・canonical stateを無検証で取り込む

### 観測された症状
- research 対象銘柄なのに、会社IRを読まず、screening 数値や外部分析だけで採用 / 見送り判断を書く
- 別AIの分析にある EPS 前提、OpenAI 連携日、AI 関連売上、同業倍率、休場日などを、
  会社IR・取引所・candidates で再確認せず research / trade に取り込む
- 「分析の方向性は合っている」ことと「records に事実として残せる」ことを混同する
- 直前の `rejected` 判定、最新 candidates からの不在、universe drop、macro context headwind などの
  system output を、override log なしに外部分析で上書きする
- 祝日中の成行注文を約定済み entry として記録し、entry price を推定で埋める

### 根本原因
- 外部 AI の整った文章を監査済み資料のように扱う
- research 対象は全銘柄で会社IR確認が必須、という前提が弱い
- source URL が貼られていても、一次情報か二次情報か、本文中に数値が存在するかを確認しない
- system output を上書きする行為を一級の decision として記録していない
- 人間報告、proposal、ledger eventの境界を曖昧にし、未報告broker状態を推定する

### 再発防止チェックリスト

- [ ] research 対象銘柄について、業種を問わず会社IRを確認したか。最低限、直近決算短信 /
      決算説明資料 / Q&A / 有価証券報告書または統合報告書 / 中期経営計画 / 株主還元関連開示を
      確認し、未確認項目を本文に残したか
- [ ] 会社IR未確認のまま `judgment.recommendation: buy` にしていないか。未確認なら`defer`または
      `reject`にして、追加確認条件を明示したか
- [ ] 外部 AI / 二次分析の結論を採用する前に、主要数値を会社IR・決算短信・決算説明資料・Q&A・
      取引所 calendar・candidates のいずれかで再確認したか
- [ ] 外部 AI セッション・証券レポート・アナリストノートを使う場合、records に原稿管理を増やさず、採用した事実と再計算結果だけを本文に残したか
- [ ] 外部 AI の出力を review 後に修正する場合、修正・未採用の判断を research 本文の確認ログに残したか
- [ ] 確認できた事実、修正した数値、未採用の二次情報を research の source verification log に分けて残したか
- [ ] EPS / PER / 配当利回り / target price は公式 EPS・配当予想・株価で再計算したか
- [ ] 候補の不在、universe drop、macro context headwind、concentration warningなどを上書きする場合、
      decision packetのevidence overrideへ人間判断の根拠と期限を残したか
- [ ] canonical ledgerの資本・集中度はcurrent + reserved exposureから再計算したか
- [ ] brokerの`open / filled / cancelled`を人間報告なしに推定していないか
- [ ] proposal/approval URLへ辿れないresultをledgerへ入れていないか
- [ ] holdings/reservationsをcanonical ledgerから読み、削除済みMarkdown globを使っていないか
- [ ] 予算、保有、予約だけを理由に、より割安な候補をscreening/research前にhard除外していないか
- [ ] ledger精密化、二重記録、realtime取得を、お買い得候補の一次情報・5年評価より優先していないか
- [ ] concentration warningを受け入れる場合、ledger overrideに理由と期限を記録したか
- [ ] 注文日が休場日または立会時間外の場合、broker-confirmed executionがない限り約定価格を推定で埋めていないか
- [ ] not-filled outcomeのlimit touchをbroker fillとして記録していないか。期限後return / missed upsideはsame-basisの観測値が揃う場合だけ補助観測として扱ったか
- [ ] fallback price observation は `decision_event_id`、`tracking_horizon`、`target_date`、`resolved_trade_date`、`price_basis`、`source_url`、`fetched_at`、`corporate_action_checked`、`same_basis_group_id`、`provisional` を持ち、basis 不一致を確定評価に使っていないか
- [ ] 外部市場予測 (例: Gartner / IDC / 証券サイトの同業倍率) は、今回の canonical fact として
      採用するなら macro context / research の source として明示し、未確認なら「判断補助・未採用」として分離したか

## 10. AP-10: hot path の YAML 読み込みを pure-Python loader で書く

### 観測された症状

- ある CLI の wall time が 17 秒。cProfile を取るまで「ロジックが遅い」と
  思い込み、YAML パースが 93% を占めていることに気付かなかった
- `yaml.safe_load(...)` を素朴に使い、libyaml backed の `yaml.CSafeLoader` に切り替えるだけで
  5 倍速くなる事実を見落とした
- YAML loaderの高速化が個別moduleに閉じ、他のrecords readerがpure-Python loaderへ戻った

### 根本原因

- `yaml.safe_load` は安全だが、デフォルトで pure-Python loader を使う。`yaml.CSafeLoader` の
  存在を明示しなければ libyaml の C 実装は呼ばれない
- hot path の判定を勘で行い、cProfile を取らずに「ロジックの 1 pass 化」「並列化」など
  micro-optimization を先に検討してしまう

### 再発防止チェックリスト

- [ ] **YAML 読み込みは必ず `from baibai_loop.yaml_io import safe_load` 経由**で書く。
      `yaml.safe_load(...)` / `yaml.load(...)` を直接呼ぶ src コードは書かない
- [ ] 新規 src モジュールで YAML 読み込みを足すときは `yaml_io.safe_load` が import されているか
      確認する。`grep -rn "yaml.safe_load" src/` は常に zero を保つ
- [ ] perf 候補を挙げる前に **cProfile で実 hot path を確定**する。
      `python -c "import cProfile; cProfile.run('...')` で cumulative time を取り、
      改善対象が cumtime の何 % か数字で示す
- [ ] perf 改善は **before/after で wall time を 5 runs 計測**し、stdev の 3σ を超える
      改善のみ「意味あり」として PR に取り込む。±1% は noise として defer

### `yaml.dump` 側

`yaml.dump` / `yaml.safe_dump` 側の hot path も同様に `yaml.CSafeDumper` を使えば加速できるが、
write side は read side ほど呼ばれないため P2 の改善候補 (cli/query.py 等)。

## 11. PR review で繰り返し指摘される類型の追跡

PR で同じ anti-pattern が 2 ラウンド以上指摘されたら、本ドキュメントの該当節を強化または
新節として追加する。直近の事例:

| PR | round | 主な anti-pattern |
| --- | --- | --- |
| #68 | 1 | AP-01 (122 条 13%、TSMC/Samsung/Kioxia 断定)、AP-02 (adv 100 倍、利確 +118% / +30% 矛盾)、AP-03 (6590 split artifact)、AP-04 (sector_relative_strength_percentile 誤読)、AP-06 (米コア PCE brief 未反映)、AP-07 (PCE 5/30 前後)、AP-08 (adv consistency 抜け道) |
| #68 | 2 | AP-01 (TSMC Capex / Sovereign AI 未確認のまま outlook で断定継続)、AP-05 (brief への分析混入)、AP-06 (outlook source_refs と brief 不整合 35 箇所)、AP-07 (OPEC+ 5/3 反映漏れ、PCE 5/28 ではなく 5/30) |
| #77 | 1 | AP-06 (outlook source_refs と春闘 fact の不整合)、AP-08 (submitted order / paper-real size 分離の validator 死角)、AP-09 (別AI分析で rejected→approved を暗黙 override、注文と約定の状態分離不足) |

## 12. 関連ドキュメント

- 思想・基本方針: [`doctrine.md`](./doctrine.md)
- 事実 / 分析の分離: [`doctrine.md#fact-analysis-separation`](./doctrine.md#fact-analysis-separation)
- macro context 仕様: [`workflow/macro.md`](./workflow/macro.md)
- research 採用判定: [`workflow/research.md`](./workflow/research.md)
- AI agent 規約 (本ドキュメントの参照経路): [`../AGENTS.md`](../AGENTS.md)
