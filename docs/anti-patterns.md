---
title: "Anti-patterns"
summary: "投資判断、data、schema、validator、AI運用で繰り返し防ぐ失敗パターンとcommit前checklist。"
doc_type: governance
status: active
last_reviewed: 2026-07-23
---

# anti-patterns

baibai-loop での AI agent 作業で観測された失敗パターン集と、再発防止のためのチェックリスト。
PR で繰り返し指摘される類型は本ドキュメントに集約し、self-review の strict gate として運用する。

このドキュメントは事後分析のためではなく **作業前 / commit 前 / PR 前のチェックリスト** として
読まれることを意図する。新規 macro context / research を書く前に必ず該当節を読み返すこと。

## 0. 全 anti-pattern 共通の根本原因

AI agent 作業で繰り返し観測される失敗の共通根本原因は以下:

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
- 122 条関税を「13% 上乗せ」と書いた (Federal Register 一次情報は 10% ad valorem)。 <!-- drift: allow-unrelated-policy-literal -->
  trade-weighted estimate の二次情報を引用元なしに断定した
- TSMC 「Capex $52-56B レンジ」「先端プロセス 70-80% 配分」「2026 年売上 +30%」を
  Q1 release から確認したと書いた (実際は Q4 transcript / IR archive 由来)
- NVIDIA 「Sovereign AI 売上 300 億 USD」「Q3-Q4 commitment 倍増」を press release 由来と
  して書いた (実際は press release では未記載、要 transcript / 10-K)
- 6590 芝浦メカトロニクス の顧客を TSMC / Samsung / Kioxia と断定した (公式製品ページで
  確認できるのは製品領域までで、顧客別売上比率は有報未確認)
- OPEC+ 5/3 statement を macro context 作成日 (5/4) に確認していなかった

### 根本原因
- 自分の事前知識ベースで「だろう」と書く habit
- 二次情報・分析記事で見た数字を一次情報の数字と区別せず引用する
- 一次情報 URL の本文を読まず source ID だけ書く

### 再発防止チェックリスト (commit 前必須)

- [ ] **すべての数値・固有名詞 (社名・組織名・地名・政策名) について、引用元 URL を文書内に明示しているか**
- [ ] その URL を実際に WebFetch / curl で取得し、本文に記載があることを確認したか
- [ ] **source の policy / rate / date / scenario が本文主張と一致しているか** (URL を貼っただけで終わらせない)
  - 例: 「Section 122 trade-weighted 13%」と書く場合、貼った Global Trade Alert source の中で
        13.0% は **15% シナリオ** の数値であり、10% 法定 (Proclamation 11012) 前提と整合しない。 <!-- drift: allow-unrelated-policy-literal -->
        10% 前提なら 11.4-11.5%、15% シナリオを使うなら法定が 15% の場合の話だと明記する <!-- drift: allow-unrelated-policy-literal -->
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
- screening runのprice系列が split 調整しているか、`record_date` ベースか `effective_date`
  ベースかを確認しない

### 再発防止チェックリスト

- [ ] screening run出力の `price_change_60d` / `price_change_20d` が **±50% を超える銘柄**は、
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
- screening runのcandidate recordにある `sector_relative_strength_percentile: 1.0` を「同業種内で最も強い銘柄」と
  解釈。実装は `_rank_to_percentiles` で sector level の rank (electronics sector が全 33
  業種中で強い) を返す。個別銘柄の同業種内相対強度ではない
- screening run / macro context schema の追加プロパティ可否を確認せず `note` / `previous_change`
  を勝手に追加 → validate error

### 根本原因
- field 名から意味を「だろう」で推測する
- engine model / 実装コードを読み直さない
- 既存サンプルとの diff を意識しない

### 再発防止チェックリスト

- [ ] screening run / macro context / research の field を新規に解釈・記述する前に、対応する
      engine modelとpublic CLI contractを読み返したか
- [ ] 計算系 field (percentile / rank / change / hit) は src 実装 (`src/baibai_engine/screening/`)
      で計算ロジックを確認したか
- [ ] DB publication viewとmodelに従い、独自構造を勝手に追加していないか
- [ ] `extra: forbid` の model に独自 key を追加していないか
- [ ] holding review / portfolio outcomeがledger・thesis・benchmark observationのimmutable IDとscalar driftを検証しているか

## 5. AP-05: fact 層と分析層の境界を曖昧にする

### 観測された症状
- 機械store（market / macro series / screening run）に「FOMC タカ派ホールドの正当化材料」「需要側
  冷却の early evidence hit」「油価高値圏粘着の構造要因」「122 条効果が顕在化」などの解釈・因果
  推論・意味付け表現を書いた (doctrine.md#fact-analysis-separation で禁止)

### 根本原因
- 事実層（機械store）と分析層（macro context / thesis）の境界を意識せず、便利な要約として書く
- doctrine.md#fact-analysis-separation の禁止表現リスト (「示唆」「背景」「受けて」「意味する」) を
  読み返さない

### 再発防止チェックリスト

- [ ] L1 / L2の機械store（market / macro series / screening run）に [`doctrine.md#fact-analysis-separation`](./doctrine.md#fact-analysis-separation) の禁止表現（因果推論・予測・意味付け・重要度評価）が 1 件も含まれていないか。禁止語リストは doctrine §6 が正本で、ここへ複写しない
- [ ] 解釈・因果推論・予測は macro context / research の分析層に移したか
- [ ] macro context で使った外部記事・統計は source metadata として残し、記事本文や網羅的 fact を repo に蓄積していないか

## 6. AP-06: macro material delta と個別判断の境界を曖昧にする

### 観測された症状
- future の macro context を判断時点の情報として使った
- stale / missing macro context を理由に、決定論的なscreeningまたは候補比較を停止した
- macroのmaterial deltaを銘柄別の事実や機械rankingへ混入した
- material deltaが個別5年期待値へ影響するのに、thesisの根拠・反証へ接続しなかった

### 根本原因
- macro contextを候補選別用のsector/ranking入力だと誤解する
- stale warningとfuture errorを区別しない
- doctrine.md の柱（事実層と分析層の物理分離、macroは判断の補助）を運用で守らない

### 再発防止チェックリスト

- [ ] macro contextを使う場合、`as_of`が判断時点より未来ではないか（futureは停止、古さはwarning）
- [ ] `inputs`のinput_id、各sectionのseries参照、fact / judgment / economic connection / material deltaのsource_ids、statusを照合したか
- [ ] core セクションに日本株ループ固有の指示（sector tilt・research優先度ヒント・sizing caution）を書いていないか。connectionのseries引用がcoreの引用範囲内か
- [ ] macro summaryをcandidateのfact、E[r]順位、機械sizingへ混入していないか
- [ ] material deltaが個別仮説に影響する場合だけ、thesisの判断と反証にsource付きで接続したか
- [ ] **機械化チェック**: macro context publishのmodel / source / future / as_of鮮度warningのnegative testを実行したか

## 7. AP-07: 公表日 / 期間 / source の最新性確認を skip する

### 観測された症状
- 米 4 月 PCE 公表予定を「5/30 前後」と書いた (BEA schedule で確認した正確な日付は
  2026-05-28 8:30 EDT)
- macro context 5/4 公開時に OPEC+ 5/3 statement を反映しなかった
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
- [ ] 次に更新すべき大型eventはsection 8の`monitoring_points`に具体日付・条件・見方の変更を残したか
- [ ] 「随時」「○月下旬」「前後」のような曖昧表現を避け、確認できた具体日付を書く
- [ ] historical calibration panel が cohort as-of 以下の master snapshot を読み、latest snapshot へ fallback していないか

## 8. AP-08: schema validator の抜け道を意識しない

### 観測された症状
- `adv_participation_pct: 0.585` (100 倍ズレ) を validator が catch しなかった
- 当初の整合チェックを `avg_turnover_oku` 不在時には silently skip するように実装、
  required field 化を忘れた → 抜け道残存
- schema 管理している nested object が未知 field を許しており、current contract 以外の値を取り込めた
- `judgment.recommendation: reject` のthesisに買い注文が紐づき、非採用判断と矛盾していた
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

- [ ] validator rule を追加・修正する場合、その rule の corner case を negative test で必ず塞ぐ。thesis の `incomplete` 条件、snapshot source の identity / 時刻 / unit 拒否、execution policy の quote / max price / cash 判定、independent review の hash 束縛、screening E[r] / FV の estimate 扱いといった個別 field の必須・拒否条件は engine model と各 negative test（`test_thesis.py` / `test_proposal_store.py` / `test_execution_policy.py` / `test_portfolio_ledger.py` 等）が正本で、本節へ網羅転記しない。追加時は最低限次の corner case を test する:
  - [ ] 関連 field が **不在** の場合 (skip / error どちらが正しいか)
  - [ ] 関連 field が **null** の場合
  - [ ] 関連 field が **0 / 負値** の場合 (decision との整合性)
  - [ ] model 管理している **nested object** が未知 field を許していないか
  - [ ] **既存 thesis** が新 rule で breakage しないか、する場合は同 commit で fix する
- [ ] ledger eventを導入・変更する場合、reservationとbuy execution、terminal orderとrelease、cash不足、guard超過、expiry後のbuy、保有超過sellをhard errorとして確認したか
- [ ] concentrationはholding market value + active reservationをledgerの`total_capital_yen`で割り、warning + 期限付きoverrideとして扱うことを確認したか
- [ ] human result CLIを変更する場合、報告なしでno write、approved proposal ID必須、missing fieldの質問、draft時canonical非変更、stale append head拒否をcontract testで確認したか
- [ ] thesisがapprovedの場合、source snapshot、scenario、independent review、execution inputが同一thesis hashに束縛されるか
- [ ] 統合reportはHTMLをreview対象にせず、findings / comparison / thesis / proposalへ別roleのcontent reviewを行い、manifest・全thesis raw/core・proposal hashの変更をstaleとして拒否するか
- [ ] `planned_limit / defer / no actionable bargain`の全経路で、購入方法または注文なしが比較結論と矛盾せず、未知source IDと手書き注文数値を拒否するか
- [ ] `planned_limit`のportfolio exposureは、共通as-of・分母・current / prospective円額・比率・閾値・fallback銘柄が必須かつ機械整合し、欠損 / null / 0 / 負値 / nested未知field / 閾値warningの過不足 / fallback warningの過不足を拒否するか
- [ ] **新 validator rule を追加するときは必ず本 docs/anti-patterns.md AP-08 の
      checklist を更新**して、次回 review で同じ穴が再発しないように記録する
- [ ] task-list validatorを変更する場合、schema違反のstatus・実在しないcalendar date・重複`task_id`をそれぞれnegative fixtureで拒否し、`task_id`一意性以外のcross-field制約や遷移監査を追加していないか
- [ ] policy literalのdrift gateを追加・変更する場合、正本の値からpatternを導出し、正本doc/codeを
      除外し、桁prefixと単位違い（円 / 株 / 件）のnegative testを持つか
- [ ] master snapshot ingestはrequested as-ofと全response `Date`の一致、必須field、normalized ticker一意性、普通株population floorをtransaction前に検証し、同日だけを置換して別日snapshotを変えないrollback testを持つか
- [ ] provider が個別 release URL の manifest を持つ場合、scheme / host / path全体をallowlistして
      lookalike host・query・fragmentを拒否し、抽出値を妥当域で検証し、矛盾する複数候補を
      hard errorにするか。manifest が公表カレンダーに追いつかない状態を無音にせず
      取得側だけを失敗させるか（読み取りは既存rowを返す）
- [ ] indicator の取得値は store 書き込み前に非有限値（NaN / ±inf）を拒否し、1 series の失敗が
      同一 pass の他 series を止めず、失敗を `provider_runs` と非0 exit の両方に残すか
- [ ] indicator registry の `plausible_min` / `plausible_max` は有限かつ順序が正しく、標準の全系列で
      両端を宣言しているか。境界値は許可し、band 外が 1 点でもあれば部分 insert せず failed
      provider run を残すか。band 変更前後に `tools/validate_macro_stores.py` で live store の
      全履歴・全 vintage が通ることを機械確認したか。複数行の途中違反を caller が catch 後に
      commit しても先行行が残らず、persistent trigger の欠落・改変・予期しない追加を
      schema version 一致だけで通さないか。`foreign_keys=OFF` の直接writerでもunknown seriesを
      拒否し、storeは空か現行schemaだけを受けて他は明確なエラーで拒否するか（過去のschemaへ戻る
      通路は持たない。schemaを進めるときはその1段だけを書く）。cloud mergeは直前schemaのread-only sourceをrollout可能にし（schema変更後の
      最初のpushは必ず1世代前のcloud copyに当たる）、同一fact keyの全payload不一致・
      source/target域外値をtransaction前後で拒否するか。
      registry generation / prune authorization stateの欠損・残留もcurrent-schema検証で止めるか
- [ ] observation を読みから外すときは delete ではなく retraction vintage を積んだか。merge の
      no-loss 契約が delete を必ず巻き戻すので、delete は「消えたように見えて次の push で戻る」
      無音の失敗になる。retraction を入れたら、store 書き換え（`trim_before_first` /
      `remove_other_sources` / `range_replacement` の全 DELETE）が retraction を残すこと、
      provider の再配信で復活すること、`delete_unchanged_vintages` が消さないこと、
      merge round-trip で両 store に伝播すること、PIT replay では retraction 前の vintage が
      見え続けることを、それぞれ test で固定したか
- [ ] macro registry の series ID 集合を変更する場合は membership generation digest を追記し、
      stale generation の refresh / merge 拒否、無許可 series DELETE trigger、件数集計から削除までの
      writer lock、pending / committed audit の各 negative testを通すか
- [ ] macro reading の計算規則は全登録系列で解決が成立し（解決不能なら fail）、実効窓を満たさない
      履歴で percentile / z-score を黙って計算しないか（開始が遅い・件数不足・**窓の期数に対する
      欠落が多い**の3条件を `insufficient_history` で null にする）。公表lagを変更するときは全系列の
      `next_print_estimate` が解決し、registry frequency と実更新 cadence が異なる系列・週次batchの
      phase・速い source 固有lag・正常な公表待ち / 1回の公表落ちの `stale` 判定が意図せず変わらず、
      月末の calendar arithmetic・calendar/business daily の土日境界・期限超過の負の
      `print_due_in_days`・margin境界・schema v1 の既発行revision・v1/v2 shape混在の拒否を
      fixtureで検証するか
- [ ] macro scorecard は未来 asof、`met` までの full-window run / `not_met` の active provider
      post-watermark run 不足、期限時点の stale 観測を hard error にし、run 完了時刻を JST の score
      asof 以前に制約するか。`met` 観測の vintage 欠落を拒否するか。観測期限と vintage cutoff を
      分離し、rules revision と両 store を identity に固定しているか。後続 context は前回 context の
      structured scorecard snapshot を exactly one で持ち、regime summary の専用 field がその
      input ID を参照し、publish が digest を再計算するか
- [ ] macro series config の `tradingview_symbol` は `EXCHANGE:SYMBOL` 形式を拒否側 fixture で検証し、
      macro read API の未知 period / granularity は 422、期間集約は各 bucket の最終観測値と件数を
      fixture で検証し、月次全履歴を返すproviderは既知の最古月・公表lagを含む最新端・
      途中月の欠落をhard errorにするか
- [ ] macro context は core 固定順10セクション + connection 1、series定義とinputへの参照、source ID、
      reading input の必須（レジーム要約からの引用・実在する rules revision・as_of との日数差）、
      base / bear / bull と各シナリオ2件以上の相異なる scorecard条件（期限は公表間隔以上18か月以内）、
      monitoring condition、core セクション2〜8内のmaterial delta、connectionのseries参照が
      coreの引用範囲内かつ core_section_ids に裏付けられていること、context_id の日付とas_ofの一致を
      negative fixtureで検証するか
- [ ] **immutable な発行済み文書の検証は、参照先が動くかどうかで層を分ける**。registry membership や
      系列の公表頻度のように後から変わる環境状態は publish 時だけ検証し、read / load 時は文書内の
      整合だけを検証する。read でも環境と照合すると、系列の退役・改名という正常な運用が過去の
      全レポートを遡って invalid にし、それを読む下流（daily batch の `screening select`）ごと
      止まる。publish が拒否する negative test と、環境が動いても read が通る positive test を
      対で持つか
- [ ] 整合チェック (cross-field consistency) は片方の欠損で skip しないよう、依存 field を
      required 化する
- [ ] 複数例外を捕捉する場合は必ず `except (A, B):` と書く。`except A, B:` は禁止。
      commit 前に `rg -n "except [A-Za-z0-9_.]+, [A-Za-z0-9_.]+" src tests` が 0 件であることを確認する
- [ ] **CLI subcommand / selection 機能を削減する場合、以下を同 commit で揃える**:
  - [ ] `src/baibai_engine/screening/cli/app.py` の subparser + `add_argument` 引数 + `main()` の dispatch
  - [ ] `src/baibai_engine/screening/cli/{__init__.py,query.py,cache.py,run.py}` の関数 / import
  - [ ] `src/baibai_engine/screening/cli/common.py` の専用 helper (`_parse_profiles_arg` のような callers が消えた helper)
  - [ ] `docs/` 全 grep (`rg <subcommand> docs/ method/ reports/`): runbook の bash example、reference の CLI 表、components / screening の説明文、`docs/reference/screening-runtime.md` の subcommand 一覧
  - [ ] `.agents/skills/`と`.claude/skills/`全grep: canonical skillとsymlinkが当該CLIを参照していないか
  - [ ] `docs/reference/screening-runtime.md` §3 (env var) / §8 (rules baseline) と `docs/workflow/screening.md` の selection block 節
  - [ ] 関連 test fixture (test_screening_cli の sweep / scorecard テスト等)
- [ ] **screening evidence pattern を削減する場合、以下を同 commit で揃える**:
  - [ ] `method/screening-rules/*.yaml` の `screening_playbooks.<playbook>` と
        `research_selection_playbook_order` から削除
  - [ ] `src/baibai_engine/screening/rules.py` の `match` 句 / PLAYBOOK_* / REASON_* / `_<playbook>_*` 関数
  - [ ] `src/baibai_engine/screening/rule_config.py` の `<Name>Playbook` class と Union 型
        (`screening_playbooks: Mapping[..., A | B | C]`) と `match` 句
  - [ ] `src/baibai_engine/screening/selection/ranking.py` の sort key match arm
  - [ ] 削除根拠は保有 outcome の calibration で示す (安易な削除で有効な割安タイプを失わない)
- [ ] **judgment-gate 系の必須 contract を追加する場合、bypass を test で塞ぐ**:
  - [ ] data 不在 label で hard trigger を回避できないか
  - [ ] label と根拠数値の不整合が catch されるか
  - [ ] `regime` key 欠落のような partial mapping が `required` 違反として catch されるか

## 9. AP-09: 外部AI・broker事実・canonical stateを無検証で取り込む

### 観測された症状
- research 対象銘柄なのに、会社IRを読まず、screening 数値や外部分析だけで採用 / 見送り判断を書く
- 別AIの分析にある EPS 前提、OpenAI 連携日、AI 関連売上、同業倍率、休場日などを、
  会社IR・取引所・screening run出力で再確認せず research / trade に取り込む
- 「分析の方向性は合っている」ことと「thesis に事実として残せる」ことを混同する
- 直前の `rejected` 判定、最新screening run出力からの不在、universe drop、macro context headwind などの
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
      取引所 calendar・screening run出力のいずれかで再確認したか
- [ ] 外部 AI セッション・証券レポート・アナリストノートを使う場合、thesis に原稿管理を増やさず、採用した事実と再計算結果だけを本文に残したか
- [ ] 外部 AI の出力を review 後に修正する場合、修正・未採用の判断を research 本文の確認ログに残したか
- [ ] 確認できた事実、修正した数値、未採用の二次情報を research の source verification log に分けて残したか
- [ ] EPS / PER / 配当利回り / target price は公式 EPS・配当予想・株価で再計算したか
- [ ] 候補の不在、universe drop、macro context headwind、concentration warningなどを上書きする場合、
      thesisのevidence overrideへ人間判断の根拠と期限を残したか
- [ ] canonical ledgerの資本・集中度はcurrent + reserved exposureから再計算したか
- [ ] brokerの`open / filled / cancelled`を人間報告なしに推定していないか
- [ ] 同一tickerのactive reservationがある間は、元注文の再表示と追加注文を区別できない`planned_limit`を新たに作っていないか
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
- YAML loaderの高速化が個別moduleに閉じ、他のYAML readerがpure-Python loaderへ戻った

### 根本原因

- `yaml.safe_load` は安全だが、デフォルトで pure-Python loader を使う。`yaml.CSafeLoader` の
  存在を明示しなければ libyaml の C 実装は呼ばれない
- hot path の判定を勘で行い、cProfile を取らずに「ロジックの 1 pass 化」「並列化」など
  micro-optimization を先に検討してしまう

### 再発防止チェックリスト

- [ ] **YAML 読み込みは必ず `from baibai_engine.yaml_io import safe_load` 経由**で書く。
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

## 11. 関連ドキュメント

- 思想・基本方針: [`doctrine.md`](./doctrine.md)
- 事実 / 分析の分離: [`doctrine.md#fact-analysis-separation`](./doctrine.md#fact-analysis-separation)
- macro context 仕様: [`workflow/macro.md`](./workflow/macro.md)
- research 採用判定: [`workflow/research.md`](./workflow/research.md)
- AI agent 規約 (本ドキュメントの参照経路): [`../AGENTS.md`](../AGENTS.md)
