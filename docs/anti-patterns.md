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
基本方針 (詳しくは [`philosophy.md`](./philosophy.md))。

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
- [ ] 単位を明示しているか (% / bp / pt / 倍 / 円 / USD)
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

## 5. AP-05: fact 層と分析層の境界を曖昧にする

### 観測された症状
- 旧 brief の `note` / `fact_memos` / `events` に「FOMC タカ派ホールドの正当化材料」「需要側
  冷却の early evidence hit」「油価高値圏粘着の構造要因」「122 条効果が顕在化」などの解釈・因果
  推論・意味付け表現を書いた (docs/design-principles.md §4.3 で禁止)

### 根本原因
- 旧 brief = 事実層 / outlook = 分析層 の境界を意識せず、便利な要約として書く
- design-principles.md §4.3 の禁止表現リスト (「示唆」「背景」「受けて」「意味する」) を
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

## 6. AP-06: 依存関係 (macro context → candidates → research) の整合性を skip する

### 観測された症状
- macro context が stale / future / scope mismatch なのに、そのまま screening / research に使った
- macro context の sector tilt と candidates の業種・exposure を確認せず、headwind を tailwind と同列に扱った
- research で macro context の caution を読み飛ばし、追加確認や低 sizing の条件を残さなかった

### 根本原因
- macro context は hard gate ではないため「見なくてもよい」と誤解する
- screening 前提の鮮度、対象 sector、tailwind / headwind を確認しない
- design-principles.md の柱 (事実層と分析層の物理分離、macro context は判断前提) を運用で守らない

### 再発防止チェックリスト

- [ ] screening 前に使う `records/01-macro-context/` が asof より未来ではないか
- [ ] `valid_until` を過ぎている場合、更新するか stale 前提のまま使う理由を selection / research で確認したか
- [ ] research の `macro_context_ref` / `candidate_ref.candidates_ref` が valid パスかつ実在するか
- [ ] `macro_context_fit.fit` と `macro_context_fit.decision_effect` が thesis / sizing / required checks に反映されているか
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
- [ ] macro context の `inputs.articles[]` / `inputs.stats_series[]` に、判断へ使った外部記事・統計 series と
      `used_for` を残したか
- [ ] 次に更新すべき大型 event は `refresh_triggers[]` に具体的に残したか
- [ ] 「随時」「○月下旬」「前後」のような曖昧表現を避け、確認できた具体日付を書く

## 8. AP-08: schema validator の抜け道を意識しない

### 観測された症状
- `adv_participation_pct: 0.585` (100 倍ズレ) を validator が catch しなかった
- 当初の整合チェックを `avg_turnover_oku` 不在時には silently skip するように実装、
  required field 化を忘れた → 抜け道残存
- schema 管理している nested object が未知 field を許しており、current contract 以外の値を取り込めた
- `research_decision.outcome: rejected` の packet で `position_sizing_overlay.paper_proxy_position_size_yen > 0` を許していたため、
  非採用 decision と sizing が矛盾していた
- `except TypeError, ValueError:` のような Python 2 風に見える except をめぐって、レビューで
  「構文エラー」なのか「Python 3.14 の PEP 758 による複数例外捕捉」なのかが混乱した。
  本 repo では可読性とレビュー容易性を優先し、複数例外捕捉は `except (A, B):` に統一する

### 根本原因
- validator を「データが揃っている前提」で実装し、欠損時の挙動を「skip」にする
- corner case (rejected / deferred / 0 値 / null) のテストを書かない
- ユーザ指摘で初めて抜け道に気付く
- runtime / formatter target の違いを確認せず、構文レビューと formatter 挙動を推測で判断する

### 再発防止チェックリスト

- [ ] validator rule を追加・修正する場合、以下の corner case の test を必ず書く:
  - [ ] 関連 field が **不在** の場合 (skip / error どちらが正しいか)
  - [ ] 関連 field が **null** の場合
  - [ ] 関連 field が **0 / 負値** の場合 (decision との整合性)
  - [ ] schema 管理している **nested object** が未知 field を許していないか
  - [ ] **既存 packet** (4/25 research 5 件など) が新 rule で breakage しないか、する場合は
        同 commit で fix する
- [ ] 以下の adv_participation 関連の具体条件を validator が catch するか、test を書いて
      確認する:
  - [ ] `avg_turnover_oku <= 0` は error (整合チェックの分母が成立しない、required な数値
        だけでは抜け道になる)
  - [ ] `position_sizing_overlay.paper_proxy_position_size_yen == 0` の場合は **`adv_participation_pct == 0`** を要求 (`position_size 0 / avg_turnover 85.4 * 100 = 0` だが `adv: 1.0` のような非ゼロを skip してしまう穴を塞ぐ)
  - [ ] **`research_decision.outcome != 'approved'` の場合は `position_sizing_overlay.paper_proxy_position_size_yen == 0` を要求** (deferred / rejected で
        正値が残ると decision と sizing が矛盾する)
  - [ ] `valuation` / `position_sizing_overlay` のような nested object は current schema の field だけを許す
- [ ] cross-field consistency rule は **依存先の field が「数値であること」だけでなく、
      「正値 (> 0) であること」を確認**する。0 / 負値で silently skip する実装は穴になる
- [ ] front matter の `avg_turnover_oku` が `candidate_ref.candidates_ref` の
      `ticker` 一致 row の値と整合しているか
      (現状は research validator が enforce する)
- [ ] trade order / execution state を導入・変更する場合、以下の corner case を確認したか
      (現状は `src/baibai_loop/validate/trade.py` が enforce する):
  - [ ] `order_intent.order_intent_id` と `orders[].origin_order_intent_id` が join できる
  - [ ] `orders[].state` は `submitted` / `broker_rejected` / `cancelled` / `expired` /
        `not_filled` / `partially_filled` / `filled` のいずれか
  - [ ] `orders[].filled_quantity <= orders[].submitted_quantity`
  - [ ] `position_state: none` で executions を持たない
  - [ ] paper proxy size と real capital / real notional / real concentration を別 field に分離
  - [ ] `capital_basis.real_capital_yen`、`capital_basis.tactical_real_budget_yen`、
        `capital_basis.paper_proxy_capital_yen` を混同していない
  - [ ] `order_price_guard_yen` を置く場合、`order_intent.quantity` /
        `position_sizing_overlay.guarded_max_notional_yen` を記録し、
        `guarded_max_notional_yen = order_price_guard_yen * quantity` と整合させたか
  - [ ] guarded notional / tactical real budget * 100 を必要時に再計算できる入力が揃っているか
  - [ ] `position_sizing_overlay.paper_proxy_position_size_yen` / `adv_participation_pct` は paper proxy の検証であり、実資金集中度の検証ではない
- [ ] research の `policy_overrides` / `decision_revisions` 配列を導入・変更する場合、以下を確認したか:
  - [ ] `policy_overrides[]` は policy field の override だけを表し、decision history を混ぜていない
  - [ ] `decision_revisions[].revision_type` が既知集合に属し、`prior_state_ref` / `prior_state` / `new_state` / `reason` の必須キーが揃う
  - [ ] `research_decision.outcome: approved` の場合、`candidate_ref` が参照した candidates repository file の対象 candidate に join できるか
  - [ ] 連続する commit で `research_decision.outcome: deferred|rejected → approved` に flip した場合、PR review で thesis / event / sizing の変更理由を確認する
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
  - [ ] `docs/` 全 grep (`rg <subcommand> docs/ records/ reports/`): runbook の bash example、reference の CLI 表、components / screening の説明文、`docs/screening/automation.md` の subcommand 一覧
  - [ ] `.claude/skills/` 全 grep: skill が当該 CLI を中核に据えていないか
  - [ ] `docs/reference/configuration.md` の関連節 (env var / profile YAML / 設定例)
  - [ ] 関連 test fixture (test_screening_cli の sweep / scorecard テスト等)
- [ ] **playbook を削減する場合、以下を同 commit で揃える** (PR #246 で 5 名レビューで指摘):
  - [ ] `records/_playbooks/<playbook>/` ディレクトリ削除
  - [ ] `records/_config/screening-rules/*.yaml` の `screening_playbooks.<playbook>` と
        `research_selection_playbook_order` から削除
  - [ ] `src/baibai_loop/screening/rules.py` の `match` 句 / PLAYBOOK_* / REASON_* / `_<playbook>_*` 関数
  - [ ] `src/baibai_loop/screening/rule_config.py` の `<Name>Playbook` class と Union 型
        (`screening_playbooks: Mapping[..., A | B | C]`) と `match` 句
  - [ ] `src/baibai_loop/screening/selection/ranking.py` の sort key match arm
  - [ ] `src/baibai_loop/screening/forward/selection_ablation.py` の `_PLAYBOOKS` tuple
  - [ ] 削除根拠は `docs/operations/backtest-runbook.md` §6 dated index で明示し、
        playbook-cohorts / selection-ablation のサンプルが「removing は安全」と
        言える数値を残す (PR #246 では cash-rich が誤って削除候補になった反省)
- [ ] **`entry_preflight.market_regime` のような judgment-gate field を追加する場合、以下の
      bypass パターンを必ず test で塞ぐ** (PR #245 で 5 名レビューで発覚した想定例):
  - [ ] `regime: unknown` のような「データ不在」label で hard_trigger を回避できないか
        (proceed が通ってしまわないか)
  - [ ] label と背後の数値 (例 `benchmark_return_20d`) の不整合 (`neutral_range` を装って実際は
        +10% rally) が catch されるか
  - [ ] gate 有効日 (`_REGIME_GATE_EFFECTIVE_DATE`) の boundary (前日が gate 対象外、当日が対象)
        を test しているか
  - [ ] backdated `published_at` で gate 有効日を回避できないか (filename / recorded_at の
        max を使うか別関数 `_gate_boundary_date` で防御)
  - [ ] partial mapping (`market_regime: {benchmark_return_20d: 0.05}` のように `regime` key を
        欠落させる) が `required` 違反として catch されるか
  - [ ] `action: exception` × waiver basis (`low_correlation` 等) なしで warning でなく error
        が出るか (warning だけだと operator が clickthrough で抜けられる)

## 9. AP-09: 外部 AI 分析を検証せず records に取り込む

### 観測された症状
- research 対象銘柄なのに、会社IRを読まず、screening 数値や外部分析だけで採用 / 見送り判断を書く
- 別AIの分析にある EPS 前提、OpenAI 連携日、AI 関連売上、同業倍率、休場日などを、
  会社IR・取引所・candidates で再確認せず research / trade に取り込む
- 「分析の方向性は合っている」ことと「records に事実として残せる」ことを混同する
- 直前の `rejected` 判定、最新 candidates からの不在、universe drop、macro context headwind などの
  system output を、override log なしに外部分析で上書きする
- 1 億円 paper proxy と実資金 position を同じ `position_size_pct` に混在させる
- 祝日中の成行注文を約定済み entry として記録し、entry price を推定で埋める

### 根本原因
- 外部 AI の整った文章を監査済み資料のように扱う
- research 対象は全銘柄で会社IR確認が必須、という前提が弱い
- source URL が貼られていても、一次情報か二次情報か、本文中に数値が存在するかを確認しない
- system output を上書きする行為を一級の decision として記録していない
- paper layer と real execution layer のサイズ概念を分離していない
- order と execution の状態遷移を trade record で区別しない

### 再発防止チェックリスト

- [ ] research 対象銘柄について、業種を問わず会社IRを確認したか。最低限、直近決算短信 /
      決算説明資料 / Q&A / 有価証券報告書または統合報告書 / 中期経営計画 / 株主還元関連開示を
      確認し、未確認項目を本文に残したか
- [ ] 会社IR未確認のまま `research_decision.outcome: approved` にしていないか。未確認なら `deferred` または
      `rejected` にして、追加確認条件を明示したか
- [ ] 外部 AI / 二次分析の結論を採用する前に、主要数値を会社IR・決算短信・決算説明資料・Q&A・
      取引所 calendar・candidates のいずれかで再確認したか
- [ ] 外部 AI セッション・証券レポート・アナリストノートを使う場合、records に原稿管理を増やさず、採用した事実と再計算結果だけを本文に残したか
- [ ] 外部 AI の出力を review 後に修正する場合、修正・未採用の判断を research 本文の確認ログに残したか
- [ ] 確認できた事実、修正した数値、未採用の二次情報を research の source verification log に分けて残したか
- [ ] EPS / PER / 配当利回り / target price は公式 EPS・配当予想・株価で再計算したか
- [ ] 直前の `rejected`、最新 candidates からの不在、universe drop、macro context headwind、実資金集中度超過などを
      上書きする場合、research front matter の `overrides` と本文に prior state / reason / evidence を残したか
- [ ] 実取引を records に残す場合、1 億円 paper proxy と real capital / real notional /
      real concentration を別 field に分けたか
- [ ] 「投資可能な実資金全体」と「当面の様子見枠」を混同していないか。様子見枠は
      `tactical_real_budget_yen` として別 field にし、`real_capital_yen` は実資金全体を分母にしたか
- [ ] `real_concentration_pct` が [`screening/principles.md §7.2`](./screening/principles.md) の hard 上限
      (単一銘柄 50% / 単一 sector 60% / cash 最低 10%) を超える場合、`overrides` に
      `type: real_concentration_cap` で記録したか。soft 推奨 (< 25% / < 40% / > 30%) を超える場合も
      本文で理由を明記したか
- [ ] 注文日が休場日または立会時間外の場合、trade は `orders[].state: submitted` とし、
      executions がない限り約定価格を推定で埋めていないか
- [ ] 2026-06-01 以降の approved research は `entry_preflight` を持ち、3pt 以上の相対劣後、stale macro、tactical exposure 50% 超を理由なし `proceed` で通していないか。`exception` は `exception_basis` を持つか
- [ ] fallback price observation は `decision_event_id`、`tracking_horizon`、`target_date`、`resolved_trade_date`、`price_basis`、`source_url`、`fetched_at`、`corporate_action_checked`、`same_basis_group_id`、`provisional` を持ち、basis 不一致を確定評価に使っていないか
- [ ] 外部市場予測 (例: Gartner / IDC / 証券サイトの同業倍率) は、今回の canonical fact として
      採用するなら macro context / research の source として明示し、未確認なら「判断補助・未採用」として分離したか

## 10. AP-10: hot path の YAML 読み込みを pure-Python loader で書く

### 観測された症状

- `screening-replay` の wall time が 17 秒。cProfile を取るまで「screening のロジックが遅い」と
  思い込み、YAML パースが 93% を占めていることに気付かなかった
- `yaml.safe_load(...)` を素朴に使い、libyaml backed の `yaml.CSafeLoader` に切り替えるだけで
  5 倍速くなる事実を見落とした
- `src/baibai_loop/validate/research/shared.py` だけが private に
  `_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)` を持っており、他 14 src 件は
  pure-Python loader のままだった (知識のサイロ化)

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
write side は read side ほど呼ばれないため P2 の改善候補 (cli/query.py / ledger/cli.py の 6 箇所)。

## 11. PR review で繰り返し指摘される類型の追跡

PR で同じ anti-pattern が 2 ラウンド以上指摘されたら、本ドキュメントの該当節を強化または
新節として追加する。直近の事例:

| PR | round | 主な anti-pattern |
| --- | --- | --- |
| #68 | 1 | AP-01 (122 条 13%、TSMC/Samsung/Kioxia 断定)、AP-02 (adv 100 倍、利確 +118% / +30% 矛盾)、AP-03 (6590 split artifact)、AP-04 (sector_relative_strength_percentile 誤読)、AP-06 (米コア PCE brief 未反映)、AP-07 (PCE 5/30 前後)、AP-08 (adv consistency 抜け道) |
| #68 | 2 | AP-01 (TSMC Capex / Sovereign AI 未確認のまま outlook で断定継続)、AP-05 (brief への分析混入)、AP-06 (outlook source_refs と brief 不整合 35 箇所)、AP-07 (OPEC+ 5/3 反映漏れ、PCE 5/28 ではなく 5/30) |
| #77 | 1 | AP-06 (outlook source_refs と春闘 fact の不整合)、AP-08 (submitted order / paper-real size 分離の validator 死角)、AP-09 (別AI分析で rejected→approved を暗黙 override、注文と約定の状態分離不足) |

## 12. 関連ドキュメント

- 思想・基本方針: [`philosophy.md`](./philosophy.md)
- 事実 / 分析の分離: [`design-principles.md`](./design-principles.md) §4
- macro context 仕様: [`components/macro-context.md`](./components/macro-context.md)
- research 採用判定: [`components/research.md`](./components/research.md)
- AI agent 規約 (本ドキュメントの参照経路): [`../AGENTS.md`](../AGENTS.md)
