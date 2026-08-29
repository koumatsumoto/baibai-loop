---
title: "Anti-patterns"
summary: "投資判断、data、schema、validator、AI運用で繰り返し防ぐ失敗パターンとcommit前checklist。"
doc_type: governance
status: active
---

# Anti-patterns

この文書は、過去に実際に踏んだfailure classから、変更対象に応じたcommit前確認を行う検索可能な
review checklistである。変更対象に対応する`AP-*`を作業前、commit前、PR前に確認し、全`AP-*`の
全文読了は要求しない。macro contextまたはresearchを書く場合も、対応する節を先に読む。

各節は次の順で読む。

1. `AP-*`見出し: 防ぐRisk
2. 「異なる失敗類型の代表例」: 同じRiskが現れた実例。網羅一覧ではない
3. 「発生理由」: checklistが必要な理由
4. 「Commit前に止める条件」: 変更に該当する項目をすべて確認する

個別fieldの厳密な契約は、各節が示すowner、model、testを正本とする。この文書は契約を再定義せず、
見落としやすい確認観点を所有する。新しいwrite-time validation ruleを追加するときは、同じfailureを
次回のreviewで止められるようAP-08も更新する。

## 0. 全anti-patternに共通する発生理由

AI agentの作業で繰り返し観測される失敗には、次の発生理由が共通する。

1. **一次情報を確認せずに二次情報・推測で書く**
2. **数値を機械的に検算しないまま記述する**
3. **データの「異常さ」に対して原因 cross-check を skip する**
4. **schema / 実装の意味を読まずに「だろう」で書く**
5. **fact 層と分析層の境界を曖昧にする**
6. **依存関係 (macro context → research) の整合性を意識しない**
7. **公表日 / source の最新性 / source の粒度を確認しない**
8. **schema validator の抜け道を意識しない**
9. **量の基準 (資本・株式・実体・期間) を確かめずに組み合わせる**

該当する`AP-*`のchecklistをcommit前に通す。速さより正しさを優先する根拠は
[`doctrine.md`](./doctrine.md)を正本とする。

## 1. AP-01: 一次情報を直接確認せず二次情報・推測で書く

### 異なる失敗類型の代表例
- 122 条関税を「13% 上乗せ」と書いた (Federal Register 一次情報は 10% ad valorem)。 <!-- drift: allow-unrelated-policy-literal -->
  trade-weighted estimate の二次情報を引用元なしに断定した
- TSMC 「Capex $52-56B レンジ」「先端プロセス 70-80% 配分」「2026 年売上 +30%」を
  Q1 release から確認したと書いた (実際は Q4 transcript / IR archive 由来)
- NVIDIA 「Sovereign AI 売上 300 億 USD」「Q3-Q4 commitment 倍増」を press release 由来と
  して書いた (実際は press release では未記載、要 transcript / 10-K)
- 6590 芝浦メカトロニクス の顧客を TSMC / Samsung / Kioxia と断定した (公式製品ページで
  確認できるのは製品領域までで、顧客別売上比率は有報未確認)
- OPEC+ 5/3 statement を macro context 作成日 (5/4) に確認していなかった
- 同じ文書の上流に「7/28〜8/3 の円高が当局介入か市場要因か一次情報で未確認」「金利差で説明でき
  ない残差の中身は不明」と書きながら、summary と公開 revision では当局介入を発生済み fact として
  断定し、残差を主機構と断じた (macro context 2026-08-12)

### 発生理由
- 自分の事前知識ベースで「だろう」と書く habit
- 二次情報・分析記事で見た数字を一次情報の数字と区別せず引用する
- 一次情報 URL の本文を読まず source ID だけ書く
- 上流で正直に置いた「未確認」が、要約・統合の過程で落ちる。**引用元を持たない断定は URL 検査に
  かからない**ので、source を確かめる checklist だけでは検出できない

### Commit前に止める条件

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
- [ ] 自分の draft の上流 (evidence / fact 層) で「未確認」「不明」「推定」と書いた事象を、下流の
      summary / judgment / 結論で確定事実として書いていないか。**一次確認に行って取れなかった
      ことは、書かない理由であって断定してよい理由ではない**。取れなかった事実自体を本文に残す

## 2. AP-02: 数値計算を機械的に検算しない

### 異なる失敗類型の代表例
- `adv_participation_pct = 0.005 / 85.4 * 100 = 0.00585%` を **0.585** と記述 (100 倍ズレ)
- 利確 target を「PER 7.35 → 16 への正常化 = entry 価格 +20-30%」と記述。実際は EPS 一定なら
  +118%、+20-30% を狙うなら PER target は 8.8-9.6
- 為替変動率を %、bp を混同するリスク

### 発生理由
- 数式を頭の中だけで処理して紙 / 電卓 / Python で再計算しない
- 「だいたい合ってる」感覚で commit する
- 単位 (%/bp、円/USD、千 / 百万 / 億) の整合性を check しない

### Commit前に止める条件

- [ ] **各数値計算について、Python / 電卓で 1 回検算した結果を文書内のコメントまたは
      `(計算: A / B * 100 = C)` の形で残しているか**
- [ ] execution policyの最大許容価格を、5年base terminal price + 累積配当と明示した要求CAGRから再計算し、合法tickへ切り下げたか。終値からの任意率やclaimed max priceを転記していないか
- [ ] 単位を明示しているか (% / bp / pt / 倍 / 円 / USD)
- [ ] portfolio outcomeでは、contribution / withdrawalだけをexternal flowとしてTWR分母へ入れ、buy/sell・reservation・配当・費用・確定税を二重にflow扱いしていないか
- [ ] 価格 → リターン換算は (新値 - 旧値) / 旧値 * 100 で計算しているか
- [ ] PER / EV/EBITDA 等の倍率変化は EPS / EBITDA 一定なら株価リターン = (target / current - 1) * 100
- [ ] 桁数 (0.005 vs 0.05 vs 0.5、1e-3 vs 1e-2 vs 1e-1) を音読で確認したか

## 3. AP-03: データの「異常さ」に対して原因 cross-check を skip する

### 異なる失敗類型の代表例
- 6590 芝浦メカトロニクス の `price_change_60d: -0.8129` (-81%) を「過剰売り」と解釈し、
  株式分割 (2026-03-01 効力 1:5) の split artifact 可能性を確認しなかった

### 発生理由
- 株価が極端に動いた (>= ±50%) のに「需給」「業績」「セクター回転」のいずれかで説明できる
  と決めつけ、corporate action (split / 合併 / TOB / 上場区分変更) の可能性を忘れる
- screening runのprice系列が split 調整しているか、`record_date` ベースか `effective_date`
  ベースかを確認しない

### Commit前に止める条件

- [ ] screening run出力の `price_change_60d` / `price_change_20d` が **±50% を超える銘柄**は、
      research に進める前に以下を確認:
  - [ ] EDINET の臨時報告書・有価証券届出書で 60 日 / 20 営業日 期間内の corporate action
        (株式分割 / 併合 / 合併 / TOB / 第三者割当) を確認
  - [ ] TDnet / 適時開示で同期間の重要発表を確認
  - [ ] J-Quants の adjustment_factor が分割を反映しているか実装で確認
- [ ] `self_range_percentile` が下位 5% 以下の銘柄も同様に corporate action を必ず確認
- [ ] portfolio outcomeで保有期間の`adjustment_factor != 1`を検出したとき、adjusted closeや0円補完で継続せず`corporate_action_unresolved`にしたか
- [ ] calibration forward で stale / missing exit を resolved return に混ぜず、delisting unknown として明示したか
- [ ] adjustment factor の観測可否と corporate-action event coverage を同一視していないか。
      event reader が価格の存在を要求していないか、`close = NULL` の権利落ち行を入れて価格、株数、
      forward return、market view の全consumerが同じfactorを使うことを確認したか
- [ ] `forward PER` と `trailing PER` の乖離が ±100% を超える場合、決算特殊要因 (税引前
      一過性 gains / losses、減損、グループ再編) の可能性を有報で確認

## 4. AP-04: schema / 実装の意味を読まずに推測で解釈する

### 異なる失敗類型の代表例
- screening runのcandidate recordにある `sector_relative_strength_percentile: 1.0` を「同業種内で最も強い銘柄」と
  解釈。実装は `_rank_to_percentiles` で sector level の rank (electronics sector が全 33
  業種中で強い) を返す。個別銘柄の同業種内相対強度ではない
- screening run / macro context schema の追加プロパティ可否を確認せず `note` / `previous_change`
  を勝手に追加 → validate error

### 発生理由
- field 名から意味を「だろう」で推測する
- engine model / 実装コードを読み直さない
- 既存サンプルとの diff を意識しない

### Commit前に止める条件

- [ ] screening run / macro context / research の field を新規に解釈・記述する前に、対応する
      engine modelとpublic CLI contractを読み返したか
- [ ] 計算系 field (percentile / rank / change / hit) は src 実装 (`engine/src/baibai_engine/screening/`)
      で計算ロジックを確認したか
- [ ] DB publication viewとmodelに従い、独自構造を勝手に追加していないか
- [ ] `extra: forbid` の model に独自 key を追加していないか
- [ ] holding review / portfolio outcomeがledger・thesis・benchmark observationのimmutable IDとscalar driftを検証しているか

## 5. AP-05: fact 層と分析層の境界を曖昧にする

### 異なる失敗類型の代表例
- 機械store（market / macro series / screening run）に「FOMC タカ派ホールドの正当化材料」「需要側
  冷却の early evidence hit」「油価高値圏粘着の構造要因」「122 条効果が顕在化」などの解釈・因果
  推論・意味付け表現を書いた (doctrine.md#fact-analysis-separation で禁止)

### 発生理由
- 事実層（機械store）と分析層（macro context / thesis）の境界を意識せず、便利な要約として書く
- doctrine.md#fact-analysis-separation の禁止表現リスト (「示唆」「背景」「受けて」「意味する」) を
  読み返さない

### Commit前に止める条件

- [ ] L1 / L2の機械store（market / macro series / screening run）に [`doctrine.md#fact-analysis-separation`](./doctrine.md#fact-analysis-separation) の禁止表現（因果推論・予測・意味付け・重要度評価）が 1 件も含まれていないか。禁止語リストは doctrine §6 が正本で、ここへ複写しない
- [ ] 解釈・因果推論・予測は macro context / research の分析層に移したか
- [ ] macro context で使った外部記事・統計は source metadata として残し、記事本文や網羅的 fact を repo に蓄積していないか

## 6. AP-06: macro material delta と個別判断の境界を曖昧にする

### 異なる失敗類型の代表例
- future の macro context を判断時点の情報として使った
- stale / missing macro context を理由に、決定論的なscreeningまたは候補比較を停止した
- macroのmaterial deltaを銘柄別の事実や機械rankingへ混入した
- material deltaが個別5年期待値へ影響するのに、thesisの根拠・反証へ接続しなかった

### 発生理由
- macro contextを候補選別用のsector/ranking入力だと誤解する
- stale warningとfuture errorを区別しない
- doctrine.md の柱（事実層と分析層の物理分離、macroは判断の補助）を運用で守らない

### Commit前に止める条件

- [ ] macro contextを使う場合、`as_of`が判断時点より未来ではないか（futureは停止、古さはwarning）
- [ ] `inputs`のinput_id、各sectionのseries参照、fact / judgment / economic connection / material deltaのsource_ids、statusを照合したか
- [ ] core / synthesis セクションに日本株ループ固有の指示（sector tilt・research優先度ヒント・sizing caution）を書いていないか。connectionのseries引用がcoreの引用範囲内か、synthesisの各forceのseries引用が名指ししたチャネルセクションの引用範囲内か
- [ ] macro summaryをcandidateのfact、E[r]順位、機械sizingへ混入していないか
- [ ] material deltaが個別仮説に影響する場合だけ、thesisの判断と反証にsource付きで接続したか
- [ ] **機械化チェック**: macro context publishのmodel / source / future / as_of鮮度warningのnegative testを実行したか

## 7. AP-07: 公表日 / 期間 / source の最新性確認を skip する

### 異なる失敗類型の代表例
- 米 4 月 PCE 公表予定を「5/30 前後」と書いた (BEA schedule で確認した正確な日付は
  2026-05-28 8:30 EDT)
- macro context 5/4 公開時に OPEC+ 5/3 statement を反映しなかった
- next_events に source_ids を紐付けず、BLS schedule などの一次情報を素通り

### 発生理由
- 「だいたいの日付」感覚で next_events に書いてしまう
- 発行直前の重要 release (前日・当日) を「まだ早すぎる」と勝手に判断して skip
- source metadata と確認対象 release の紐付けを必須運用していない

### Commit前に止める条件

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

### 異なる失敗類型の代表例
- `adv_participation_pct: 0.585` (100 倍ズレ) を validator が catch しなかった
- 当初の整合チェックを `avg_turnover_oku` 不在時には silently skip するように実装、
  required field 化を忘れた → 抜け道残存
- schema 管理している nested object が未知 field を許しており、current contract 以外の値を取り込めた
- `judgment.recommendation: reject` のthesisに買い注文が紐づき、非採用判断と矛盾していた
- `except TypeError, ValueError:` のような Python 2 風に見える except をめぐって、レビューで
  「構文エラー」なのか「Python 3.14 の PEP 758 による複数例外捕捉」なのかが混乱した。
  本 repo では可読性とレビュー容易性を優先し、複数例外捕捉は `except (A, B):` に統一する

### 発生理由
- validator を「データが揃っている前提」で実装し、欠損時の挙動を「skip」にする
- corner case (rejected / deferred / 0 値 / null) のテストを書かない
- unresolved cohort を aggregate から silent drop し、coverage が完全であるかのように扱う
- ユーザ指摘で初めて抜け道に気付く
- runtime / formatter target の違いを確認せず、構文レビューと formatter 挙動を推測で判断する

### Commit前に止める条件

#### 共通validator

- [ ] validator rule を追加・修正する場合、その rule の corner case を negative test で必ず塞ぐ。thesis の `incomplete` 条件、snapshot source の identity / 時刻 / unit 拒否、planning limitの価格 / cash 判定、independent review の hash 束縛、screening E[r] / FV の estimate 扱いといった個別 field の必須・拒否条件は engine model と各 negative test（`test_thesis.py` / `test_position_result_service.py` / `test_portfolio_ledger.py` 等）が正本で、本節へ網羅転記しない。追加時は最低限次の corner case を test する:
  - [ ] 関連 field が **不在** の場合 (skip / error どちらが正しいか)
  - [ ] 関連 field が **null** の場合
  - [ ] 関連 field が **0 / 負値** の場合 (decision との整合性)
  - [ ] model 管理している **nested object** が未知 field を許していないか
  - [ ] **既存 thesis** が新 rule で breakage しないか、する場合は同 commit で fix する
  - [ ] decisionに応じて必須・禁止が切り替わる分類fieldは、必須時の欠落・未定義値・禁止時の混入をすべて拒否するか

#### 判断・operation境界

- [ ] 人間確認なしで完了できる operation 分岐は、専用の completion reason と canonical artifact evidence を必須にし、`not applicable` 等を human confirmation field へ書く抜け道、別 session kind での流用、evidence 件数の矛盾を negative test で拒否するか
- [ ] rebuildable publication を再利用する gate は、外部 summary の schema・terminal state・artifact ID を exact に検証し、run と selection の両方を同一の clean application commit に束縛するか。長い計算は開始時 commit を publication 直前に再照合し、dirty tree・HEAD 変更・片方だけ provenance 欠損を current code 扱いしない negative test があるか
- [ ] macro context の確率検証は float 等値比較でなく整数化算術で書き、値がある場合の境界（0.00 / 0.95 / 刻み外 / 部分欠落）を negative test で塞ぐ。散文品質を cardinality や token matching で代理判定する gate を足していないか
- [ ] ledger eventを導入・変更する場合、reservationとbuy execution、terminal orderとrelease、cash不足、guard超過、expiry後のbuy、保有超過sellをhard errorとして確認したか
- [ ] concentrationはholding market value + active reservationをledgerの`total_capital_yen`で割り、warning + 期限付きoverrideとして扱うことを確認したか
- [ ] human result CLIを変更する場合、報告なしでno write、buy assessmentのdecision reference必須、missing fieldの質問、draft時canonical非変更、stale append head拒否をcontract testで確認したか
- [ ] thesisがapprovedの場合、source snapshot、scenario、independent review、execution inputが同一thesis hashに束縛されるか
- [ ] current decision の eligibility clock はoperation入口で1回だけ取得したtimezone-aware instantを全validationへ渡し、review等のevent timestampやartifactのas-ofへ差し替えていないか。naive clock、expiry直前・exact expiry・直後をnegative testで固定したか
- [ ] 統合判断はHTMLをreview対象にせず、comparison / thesis / assessmentへ別roleのcontent reviewを行い、全thesis core hashとreviewの変更をstaleとして拒否するか
- [ ] `planned_limit / defer / no actionable bargain`の全経路で、購入方法または注文なしが比較結論と矛盾せず、未知source IDと手書き注文数値を拒否するか
- [ ] `planned_limit`のportfolio exposureは、共通as-of・分母・current / prospective円額・比率・閾値・fallback銘柄が必須かつ機械整合し、欠損 / null / 0 / 負値 / nested未知field / 閾値warningの過不足 / fallback warningの過不足を拒否するか
- [ ] machine judgment が下流の作業範囲を決める gate は、その集合を**判断artifactからDBで再解決**して検査し、workspace / manifest / draft の自由編集で広げられないことを negative test で塞いだか。手書き側は読み取り用の記録に留め、authorization source にしない（`research prepare --shortlist-id` は Shortlist `selected` を admission 可能集合とし、各 gate が stored shortlist から再解決する）
- [ ] 前提を再証明する gate は、**入口が課した前提集合の全体**を見ているか。部分集合しか見ない再証明は、残りの前提を宣言で飛ばす経路として残る（`holding-prepare` は保有と as-of の 2 つを課すので、gate も同じ 2 つを 1 つの共有 helper から見る）
- [ ] 鮮度の pin は、**その purpose が実際に依存する field を覆っているか**。`append_head` は `ledger_event` しか数えず、market price は別 table を丸ごと入れ替えるので、pin が一致したまま価格観測日だけが動く。覆えない残りは「最後の関門だけが見る」と正直に書き、gate が見ていない範囲を over-claim しない
- [ ] **その修正が案内する復旧手順を実際に最後まで通したか。** 途中までしか復旧しない手順は、operator を最も高コストな工程へ誘導したうえで最後の関門で落とす（`holding-prepare --force` は `<ws>/<ticker>/` を再生成しないので、`thesis-scaffold --force` まで案内し、残った draft を `status` に出す）
- [ ] その gate に**分岐（purpose / mode / kind）で無効化される経路**がある場合、分岐先も同じ強さで対象を store に対して証明するか。「この分岐には gate が要らない」は、その分岐を宣言するだけで gate を外せる形で残る（`purpose: holding_review` は Shortlist 束縛を持たない代わりに、対象が canonical ledger の保有であることを各 gate で再照合する）
- [ ] その gate は**下流で最初に不可逆な資源を使う手前**に置いたか。Research Gateのadmissionはresearch開始前、buy assessmentの検証はhuman-confirmed ledger draft作成前に置く
- [ ] generator が入力を読み、出力directoryへ固定名のartifactを書く場合、入力pathが出力directory内へ解決されて自分自身を上書きしないことを、書き込み前のvalidationとnegative testで保証したか
- [ ] **新 validator rule を追加するときは必ず本 docs/anti-patterns.md AP-08 の
      checklist を更新**して、次回 review で同じ穴が再発しないように記録する

#### Lake・release・generation

- [ ] lake Raw lineageはcontent digestだけでなくprovider・dataset・request rangeをmetadataと照合し、対象partitionと交差しないrangeを拒否するか
- [ ] immutable dataset / release manifestを変更する場合、rootとnested objectの未知field、
      required fieldの欠落・null・0/負値、layer別source IDの必須/禁止、重複partition/object key、
      contract versionごとのordered partition layoutと各partitionのexact key集合、object keyの
      dataset/contract/partition/content hash不一致、totals不一致、duplicate JSON key、path
      traversalをそれぞれnegative testでfail closedにするか
- [ ] 固定releaseのreaderやhydrationを変更する場合、pointerを実行中に1度しか読まないこと、
      pointerのmid-run変更が入力releaseを変えないこと、release manifest digest / pointer
      manifest key / dataset manifestとrelease entryの不一致 / 未受入contract version /
      object digest・byte数・row数・Arrow schemaの不一致 / 未publishのmonth要求 / path
      traversalをそれぞれfail closeにするnegative testを持つか。hydrationは積んだ行数が
      release manifestのpublish行数と一致しなければ失敗し、同一directoryのdurable atomic
      replace以外では公開せず、失敗時は直前のstoreを壊さないことと、credentialがSQL文・
      例外・metadataへ出ないことをtestで固定したか
- [ ] market storeをreleaseへ束縛するgateは、判断対象と同じsealed SQLite generation内の
      `lake_store_origin`を使い、分離可能なfileやlive pathをauthorityにしないか。
      「再構築可能」を名乗る場合はconsumerが読むlake外のledgerを含む全入力bytesと、そのretention
      rootが実在するかを列挙したか。一部tableの一致やdigestだけを完全な再構築保証へ読み替えないか。
      exact replayがT1〜T3の成果に不要なら、新しい永続stateやblockerを足さず、保持済みoutputのintegrityと
      現在の完全storeからの再buildで済ませるか。dehydrateはDELETE開始前にembedded originとresolved
      releaseのID・digest一致、およびcurrent指定時のpointer再確認を行い、不一致時のDB不変をnegative
      testで固定したか。store-wide originを進めるhydrateはtarget releaseの全datasetを対象とし、partial
      hydrateはsame-origin repairだけに限定したか。cross-release hydrateでtargetが持たないlake datasetの
      rowを旧storeから引き継がず、origin更新前に拒否するnegative testがあるか
- [ ] calibration snapshotを変更する場合、panel / diagnostics / forwardのrow contract、primary key、
      as-of、measurement policyをwrite/read両側で検証し、全cohortが同じrules hashを持つことを確認してから
      `current.sqlite`をatomic replaceするか。旧snapshotはruntime migrationせず再構築するか
- [ ] dataset registryへdatasetを足す、または dataset ごとの契約項目を増やす場合、契約値が
      「行数などデータの現状」から導出されていないか（reader が manifest の layout を契約と
      突き合わせるので、データ由来の契約は table が育った日に無言で変わり release を拒否し始める）。
      contract version据え置きでpartition layoutが変わる manifest を拒否するか。coverage完全性を
      「行の存在」から推定していないか（取得しなかった記録と存在しない記録は同じ不在を残す）。
      population・coverage floorなど「持たない dataset がある」項目は、欠測をskipせずfail closeするか。
      cadenceや先取り公表の差をprofile単位の単一閾値で潰していないか。行を持たないdatasetを
      build失敗と区別するか。既存datasetのobject key / digestが不変であることを実exportで確認したか
- [ ] 入力保証を根拠にproduction変更を許可するgateを追加・変更する場合、保証水準の名前が「何を再実行できるか」を
      一意に指すか（前のproducerの出力archiveを上流入力と同じ語で呼ばない）。結論を構成する全role（panel /
      diagnostics / forward）の最弱から導くか。manifestの記述だけでなくsource closureの現存とdigestを同一実行内で
      確認するか。開示値は全run purposeで実測し、未計測を「欠けなし」に見える既定値で埋めないか。purpose限定の
      blockerが他のpurposeへ漏れていないか。retained panel + trace-only forward、archiveのみ、archive削除・改変、
      diagnosticでの非block、空sourceをそれぞれnegative testで固定したか
- [ ] 固定したL1 releaseを渡して読ませるAPIを追加・変更する場合、渡されたreleaseだけで
      答えを閉じるか。rowだけでなく、rowの検証に使う policy / contract / identity も渡された世代から取るか。
      current pointerを別世代へ動かした後、および pointer を削除した後に同じ結果が読めることをtestで固定したか。
      **consumer側もそのAPIを使っているか** — 世代を渡せるようにしただけで呼び手がdirectory渡しのままなら、
      1回の測定が cohort列挙・rows・identity を別々のcurrentから読む。個々のreadは全てvalidなのでdigestも
      schemaも反対せず、報告だけが何も生成していない数値になる。run途中でpointerを動かすbarrier testを置いたか
- [ ] 部分範囲を再計算する操作（`--force`等）は、範囲外の既存生成物を黙って落とさないか。公開直前に
      「今serveしている集合」と「これからserveする集合」を比較し、差分があれば名指してfail closeするか。
      範囲がstoreを包含する場合は通ることも併せてtestしたか（否定側だけのtestは経路の全滅を隠す）。
      比較対象が読めない状態（壊れたroot等）では、推定で埋めずにその場での置換自体を拒否したか。
      **稀な障害の復旧経路を通常経路の分岐として持つと、毎日の経路が常時その分岐を抱える。**
      復旧は別の出力先へ作り直して入れ替える手順に寄せ、通常経路の状態数を増やさないか
- [ ] wireへ出す集約値は、参照先から導出して検証するか、出さないか。writeされるだけで誰も読まない
      summary fieldは、alternate writerが任意の値を名乗れて誰も誤りと言えないので削除する
- [ ] 可用性・充足性の観測値は「非該当」「充足」「不足」を区別するか。検証対象が無い場合を「充足」と
      書くと、最も素性の弱い対象が最も確かに見える。検証I/Oはその実行が扱う対象へ限定したか
- [ ] 壊れたrootのrecovery操作を追加・変更する場合、対象root以外（previous・健全なmanifest）のidentityと
      closureが操作前後で完全一致することをtestで固定したか。復旧のためにdirectory単位でmanifestを退避すると、
      無関係なpinがunresolvedになりGCが恒久停止する。**rootを退避したstoreが「未公開のstore」と同じ姿に
      なっていないか** — 両者が同じ答えを返すなら、次の通常実行はそれを空のstoreと読んで書き潰す。
      publish済みの痕跡（manifest等）が残る限りfail closeし、退避が失敗しても壊れたままへ収束するか
- [ ] wireのschema契約をdrift gateで固定する場合、readerが実際に比較する要素（Arrow metadataのdataset /
      contract version / row type stamp等）を署名へ入れたか。列を変えずrow型名だけを変えるmutationでgateが赤くなるか。
      失敗メッセージが実測値をそのまま出して「記録値を上書きすれば緑になる」と読める形になっていないか
      （記録は版ごとの意味なので、上書きは同じ版に2つの形を持たせる。正しい修復は版を上げて追記する側である）
- [ ] 実装のdigestをfingerprintへ入れる場合、値を動かさない差分（コメント・docstring・整形）で
      動かないか。bytesのhashは31%が提示の差で、1行のdocstringが全cohortを捨てさせる。またその
      digestが1 partitionごとに取られるなら、コストを旧実装と比べたか（AST parseはbytes hashの216倍）
- [ ] 取得の記録（`source_coverage`）を、それが記述する行とは別の場所から数え直していないか。
      行がreleaseから、記録がstore mergeから届くようになった後、targetの行を数えて記録へ書き戻すと
      「まだ見えていない」が「取得して0件だった」に化ける。zeroとunknownを分ける契約がある
      datasetでは、記録はそれを書いたfetchの数値を運ぶか。実行数へ**引き上げるだけ**で決して
      引き下げないか（引き下げた記録は、行が戻っても小さいままで、gateが以後の実行を止める一方
      再取得は計画されない）。取得の記録が追記専用だと仮定していないか——失敗した取得が範囲を
      撤回する設計なら、keyによるunionはそれを復活させる
- [ ] market lakeのcomplete coverageはtable自身の`MIN..MAX`だけで自己充足させず、profileが固定する
      history boundary・row floor・population floorをrelease時に再検証するか。新鮮な1日1row、
      leading history欠損、大幅なrow/population regressionをcurrent候補にしないnegative testがあるか

#### 横断validator

- [ ] task-list validatorを変更する場合、schema違反のstatus・実在しないcalendar date・重複`task_id`をそれぞれnegative fixtureで拒否し、`task_id`一意性以外のcross-field制約や遷移監査を追加していないか
- [ ] policy literalのdrift gateを追加・変更する場合、正本の値からpatternを導出し、正本doc/codeを
      除外し、桁prefixと単位違い（円 / 株 / 件）のnegative testを持つか

#### Calibration・screening・market data

- [ ] calibration coverage の対象 row は diagnostics の件数だけでなく row identity も保存し、件数不一致・未知 status・感度計算不能を fail closed にするか。diagnostic-only panel は directory と provenance hash を production から分け、`production_decision` では authority flag 単独でなく variant・入力窓・全 row の quality を固定 tuple として照合する negative test を持つか
- [ ] `priced_master_without_universe` の対象 return が未解決でも値を推定せず、全対象 row への全損 / resolved 母集団中央値の両側代入で結論方向を判定するか。方向 split、diagnostics 件数と row identity の不一致、candidate partition 不一致、未知 unresolved status をそれぞれ fail closed にする negative test があるか
- [ ] calibration total return は FY 行なし / `DivAnn: null` / `DivAnn: 0` を区別し、前 2 つを 0 円に補完していないか。同一 FY の訂正を重複加算せず、最新 non-null 訂正が負値・非有限なら古い正常値へ fallback せず拒否するか。DPS と entry price を同じ adjustment-factor basis へ揃える split negative test があるか。total-return 欠損が price-only metric を欠損または改変せず、optional metric を required にした run だけが、status 欠落・非 mapping・未知値を含めて fail closed になるか
- [ ] E[r] 水準の表示 artifact は eligible な production required scope からだけ生成し、quintile 境界・basis・rules hash・E[r] model version・timezone・固定45日期限を検証するか。表示対象 operative run の不変 method identity も照合し、run identity 不明、欠損・不正・method不一致・期限切れを古い値や手書き値へ fallback せず文脈全体を非表示にし、表示値を個別予測または ranking input として扱わないか
- [ ] 報告空売り残高は disclosure / calculation の両日、provider row ordinal、取消rowをlossなく保存し、完全重複や同率最新stateを勝手に合算・上書きしないか。PandasのNaN / NaTを文字列factへ変換せず、公式dataset floorからの連続coverageがないtickerを無報告0へ補完しないnegative testがあるか
- [ ] 日次ranked-set履歴はselection欠損と空ranked setを別statusの空recordとして発行し、FV有無やcandidate全件からmembershipを推定しないか。as-of / filename不一致、重複日、invalid memberをnegative testで拒否するか
- [ ] calibration quality condition は current/prior の開示時点を混ぜず、欠損を不充足へ補完していないか。6成分未満の composite を null にし、cache の optional boolean が空欄 / `true` / `false` 以外なら fail closed にする negative test があるか
- [ ] calibration の株主還元変化列は同一 FY の最新 revision を選んでから null を判定し、DPS・株数を同じ split basis へ揃えているか。3 FY 不足、DPS YoY の非有限値、株数減少 streak の範囲外、optional boolean の不正 token、change composite と成分の矛盾を cache read で fail closed にする negative test があるか
- [ ] calibration の利益正規化列は同一 FY の最新 revision を選び、最新 null から旧値へ fallbackせず、赤字年を含む連続3/5 FYとsplit basisを固定しているか。平均EPS非正、FY不足・不連続、PER非正・非有限、cycle percentile範囲外・flag矛盾、不正bool、self-range session負値、variant provenance混在をfail closedまたは明示nullにするnegative testがあるか
- [ ] productionで正規化PERを表示する場合、通常の1200日bar / 730日summary coverageだけで長期入力を充足扱いにせず、2200日のFY履歴とsplit basisをticker/date密度まで別々にfail closedで確認するか。日次runはFY行と非1のadjustment factorだけを疎に読み、欠損を旧EPS・未調整EPS・warning真偽へ補完しないか。bootstrap、片方だけ欠けるnegative coverage、古い期間の横断欠損、SQLite-only runをtestで固定したか
- [ ] screeningの財務fieldを追加・必須化する場合、日付coverageだけで投入済みとみなさずexact as-ofのticker母集団でnull/部分population/field組合せをfail closedにするか。補修は欠損tickerの既知開示日へ限定し、広い正常coverageを無効化せず、chunk中断後の残件再計画と候補全滅前の停止をnegative testで固定したか
- [ ] master snapshot ingestはrequested as-ofと全response `Date`の一致、必須field、normalized ticker一意性、普通株population floorをtransaction前に検証し、同日だけを置換して別日snapshotを変えないrollback testを持つか
- [ ] cadence・母集団が変わる market source は旧新の date domain を write-time に分離し、
      境界外日付、payload date 不一致、同一日/ticker 重複、旧新 table への二重計上を negative
      fixture で拒否するか。公表前の empty coverage が境界日の実データを永久に隠さず、旧 metric は
      旧 source だけを読むことを固定したか
- [ ] EDINET metric snapshotを差分再利用する場合、rowの抽出・文書状態revision必須、訂正eventを含むsource identity完全一致、target以下のbaseline選択、failed skip、hard parser failure拒否、同日失敗時の正常snapshot保持、最新ok coverageのrange/error/count矛盾時のfail-closedをnegative testで固定したか
- [ ] EDINET の投資有価証券を追加・変更する場合、`InvestmentSecurities` exact local name、連結優先、zero-like、類似 BS / 売却損益 / CF tag の除外、非負・有限の write-time validation、asset-backed ratio の source field / 単位整合、cache schema 更新を positive / negative test で固定したか
- [ ] provider が個別 release URL の manifest を持つ場合、scheme / host / path全体をallowlistして
      lookalike host・query・fragmentを拒否し、抽出値を妥当域で検証し、矛盾する複数候補を
      hard errorにするか。manifest が公表カレンダーに追いつかない状態を無音にせず
      取得側だけを失敗させるか（読み取りは既存rowを返す）
- [ ] calibration panel の信用需給列は、`margin_short_to_adv` を公表週つき非負値、
      規模帯内 percentile を `[0,1]` かつ `in_population`・時価総額・元軸つき、
      realized volatility を有限非負として read 時に検証し、列追加時は cache schema を更新する

#### Workflow trust

- [ ] GitHub Actions のtrust gateは`.yml` / `.yaml`の両方を走査し、dispatch inputの`run:`直接展開とvalidation step外の参照、step env外のsecret context、未承認・tag/branch参照の外部Actionを拒否するか。secretを使うpre-merge acceptanceはrepository ownerが付ける固定label、same-repository PR、event-bound exact head SHA、checkout credential非保持、credential-bearing final stepとworkflow/jobの継承execution contextを一体で固定し、owner判定・head repository・SHA source・credential保持・custom shell・container・runnerを緩めるnegative fixtureを持つか。日付の形式・順序、bracket形式のexpression、inline `uses:`、欠落したrelease commentをnegative fixtureで固定したか

#### Indicator・source

- [ ] indicator の取得値は store 書き込み前に非有限値（NaN / ±inf）を拒否し、1 series の失敗が
      同一 pass の他 series を止めず、失敗を `provider_runs` と非0 exit の両方に残すか
- [ ] indicator registry の `plausible_min` / `plausible_max` は有限かつ順序が正しく、標準の全系列で
      両端を宣言しているか。境界値は許可し、band 外が 1 点でもあれば部分 insert せず failed
      provider run を残すか。band 変更前後に `baibai-batch validate-macro-stores` で live store の
      全履歴・全 vintage が通ることを機械確認したか。複数行の途中違反を caller が catch 後に
      commit しても先行行が残らず、persistent trigger の欠落・改変・予期しない追加を
      schema version 一致だけで通さないか。`foreign_keys=OFF` の直接writerでもunknown seriesを
      拒否し、storeは空・直前schemaからの一段移行・現行schemaだけを受けるか（複数世代のmigration
      は持たず、schemaを進めるときは実在storeに必要な1段だけを書く）。cloud mergeは直前schemaのread-only
      sourceをrollout可能にし（schema変更後の最初のpushは必ず1世代前のcloud copyに当たる。
      ただし列集合が一致する変更に限る）、同一fact keyの全payload不一致・source/target域外値を
      transaction前後で拒否するか。撤回済みrowは値についての主張ではないのでband検査の対象外か。
      registry generation stateの欠損・改変もcurrent-schema検証で止めるか

#### Store publish・lake integrity

- [ ] 破壊的な運用コマンドは冪等か compare-and-swap で守られているか。2 回流して結果が変わる
      コマンドは、再実行という最も起きやすい操作で正本データを黙って壊す
- [ ] market storeをcloudからlocalへunionして再発行する場合、cleanな財務range coverageを
      source / targetの全key（shared / source-only / target-only）でmerge前後に実rowへ再計数し、
      入力時の偽claimを拒否したうえでunion後のtarget countを実rowから再生成するか。
      `ok` + errorなしと`partial` / `failed` + errorありを完全分類してunknown / hybridを拒否し、
      failure provenanceの完全一致は拒否しないか。財務fieldのNULL例外はcloud欠損→
      完全再構築local保持の方向だけか。
      全writerがdownload時のR2 ETagをbackupと最終PutObjectの条件へ渡し、手動publish後に
      stale daily writerが到着する逆順と最後のversion確認後のraceもprecondition failureで
      no-overwriteになるnegative testを持つか
- [ ] lakeのmanifest / pointer JSONは共通strict parserだけを通し、rootとnestedのduplicate
      keyを拒否し、parse前のwire size上限を持ち、validation errorへpayload値を展開していないか。
      logical manifestからR2 ETagを
      分離し、nested mappingをparse後に変更できないか。lineageはtyped `SourceRef`でsource kind・
      key・digest・versionを検証し、magic prefixや架空releaseを使っていないか。production releaseは
      profileごとのrequired dataset・contract・coverage・trusted clock基準のfreshness/skew・manifest
      budgetを満たすか
- [ ] observation を読みから外すときは delete ではなく retraction vintage を積んだか。merge の
      no-loss 契約が delete を必ず巻き戻すので、delete は「消えたように見えて次の push で戻る」
      無音の失敗になる。retraction を入れたら、store 書き換え（`trim_before_first` /
      `remove_other_sources` / `range_replacement` の全 DELETE）が retraction を残すこと、
      provider の再配信で復活すること、`delete_unchanged_vintages` が消さないこと、
      merge round-trip で両 store に伝播すること、PIT replay では retraction 前の vintage が
      見え続けることを、それぞれ test で固定したか。**撤回した値から計算済みの derived 系列**が
      残らないこと（入力が消えるので再計算では直らない）も確認したか

#### Macro

- [ ] macro registry の series ID 集合を変更する場合は membership generation digest を追記し、
      stale generation の refresh / merge 拒否と、件数集計から削除までの writer lock を確認するか
- [ ] macro reading の計算規則は全登録系列で解決が成立し（解決不能なら fail）、実効窓を満たさない
      履歴で percentile / z-score を黙って計算しないか（開始が遅い・件数不足・**窓の期数に対する
      欠落が多い**の3条件を `insufficient_history` で null にする）。公表lagを変更するときは全系列の
      `next_print_estimate` が解決し、registry frequency と実更新 cadence が異なる系列・週次batchの
      phase・速い source 固有lag・正常な公表待ち / 1回の公表落ちの `stale` 判定が意図せず変わらず、
      月末の calendar arithmetic・calendar/business daily の土日境界・期限超過の負の
      `print_due_in_days`・margin境界・schema v1 の既発行revision・v1/v2 shape混在の拒否を
      fixtureで検証するか
- [ ] macro scorecard は未来 asof を拒否し、観測期限と vintage cutoff を分離しているか。期限内の
      最初の成立を `met`、期限前の不成立を `pending`、期限到達後の不成立を `not_met` とし、rules
      revision と両 store を出力 identity に固定しているか
- [ ] macro series config の `tradingview_symbol` は `EXCHANGE:SYMBOL` 形式を拒否側 fixture で検証し、
      macro read API の未知 period / granularity は 422、期間集約は各 bucket の最終観測値と件数を
      fixture で検証し、月次全履歴を返すproviderは既知の最古月・公表lagを含む最新端・
      途中月の欠落をhard errorにするか
- [ ] macro context は core 固定順10セクション + connection 1、series定義とinputへの参照、source ID、
      reading input の必須（レジーム要約からの引用・実在する rules revision・as_of との日数差）、
      base / bear / bull と各シナリオ2件以上の相異なる scorecard条件（期限は公表間隔以上18か月以内）、
      monitoring point、core セクション2〜8内のmaterial delta、connectionのseries参照が
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

#### 横断変更

- [ ] 複数例外を捕捉する場合は必ず `except (A, B):` と書く。`except A, B:` は禁止。
      commit 前に `rg -n "except [A-Za-z0-9_.]+, [A-Za-z0-9_.]+" src tests` が 0 件であることを確認する
- [ ] **CLI subcommand / selection 機能を削減する場合、以下を同 commit で揃える**:
  - [ ] `engine/src/baibai_engine/screening/cli/app.py` の subparser + `add_argument` 引数 + `main()` の dispatch
  - [ ] `engine/src/baibai_engine/screening/cli/{__init__.py,query.py,cache.py,run.py}` の関数 / import
  - [ ] `engine/src/baibai_engine/screening/cli/common.py` の専用 helper (`_parse_profiles_arg` のような callers が消えた helper)
  - [ ] `docs/` 全 grep (`rg <subcommand> docs/ method/ reports/`): runbook の bash example、reference の CLI 表、components / screening の説明文、`docs/reference/screening-runtime.md` の subcommand 一覧
  - [ ] `.agents/skills/`と`.claude/skills/`全grep: canonical skillとsymlinkが当該CLIを参照していないか
  - [ ] `docs/reference/screening-runtime.md` §3 (env var) / §8 (rules baseline) / §select の判断境界
  - [ ] 関連 test fixture (test_screening_cli の sweep / scorecard テスト等)
- [ ] **screening evidence pattern を削減する場合、以下を同 commit で揃える**:
  - [ ] `method/screening/rules/*.yaml` の `evidence_patterns.<pattern>` と
        `evidence_pattern_order` から削除
  - [ ] `engine/src/baibai_engine/screening/rules.py` の `match` 句 / EVIDENCE_PATTERN_* / REASON_* / `_<pattern>_*` 関数
  - [ ] `engine/src/baibai_engine/screening/rule_config.py` の `<Name>EvidencePattern` class と Union 型
        (`evidence_patterns: Mapping[..., A | B | C]`) と `match` 句
  - [ ] `engine/src/baibai_engine/screening/selection/ranking.py` の sort key match arm
  - [ ] 削除根拠は保有 outcome の calibration で示す (安易な削除で有効な割安タイプを失わない)
- [ ] **domain語彙をrenameする場合、new-write / read projection / behavior assetをatomicに揃える**:
  - [ ] producer、consumer、Web contract、skill、method、current docsから旧identifierを除去する
  - [ ] immutable historyはrewriteせず、旧keyを読むadapter pathだけを明示allowlistする
  - [ ] `check_legacy_semantics.py`へ旧identifierのnegative testとadapterのpositive testを追加する
- [ ] selectionのranked setをnew-writeへ追加・変更する場合、run identity / candidate membership / native E[r]、表示E[r]・FV・価格、順位、review capを同じ発行境界で照合するか。不整合なrowをShortlistへ焼き込めないnegative testがあるか
- [ ] **judgment-gate 系の必須 contract を追加する場合、bypass を test で塞ぐ**:
  - [ ] data 不在 label で hard trigger を回避できないか
  - [ ] label と根拠数値の不整合が catch されるか
  - [ ] `regime` key 欠落のような partial mapping が `required` 違反として catch されるか

## 9. AP-09: 外部AI・broker事実・canonical stateを無検証で取り込む

### 異なる失敗類型の代表例
- research 対象銘柄なのに、会社IRを読まず、screening 数値や外部分析だけで採用 / 見送り判断を書く
- 別AIの分析にある EPS 前提、OpenAI 連携日、AI 関連売上、同業倍率、休場日などを、
  会社IR・取引所・screening run出力で再確認せず research / trade に取り込む
- 「分析の方向性は合っている」ことと「thesis に事実として残せる」ことを混同する
- 直前の `rejected` 判定、最新screening run出力からの不在、universe drop、macro context headwind などの
  system output を、override log なしに外部分析で上書きする
- 祝日中の成行注文を約定済み entry として記録し、entry price を推定で埋める

### 発生理由
- 外部 AI の整った文章を監査済み資料のように扱う
- research 対象は全銘柄で会社IR確認が必須、という前提が弱い
- source URL が貼られていても、一次情報か二次情報か、本文中に数値が存在するかを確認しない
- system output を上書きする行為を一級の decision として記録していない
- bargain assessment、人間報告、ledger eventの境界を曖昧にし、未報告broker状態を推定する

### Commit前に止める条件

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
- [ ] buy assessmentのdecision referenceへ辿れないresultをledgerへ入れていないか
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

### 異なる失敗類型の代表例

- ある CLI の wall time が 17 秒。cProfile を取るまで「ロジックが遅い」と
  思い込み、YAML パースが 93% を占めていることに気付かなかった
- `yaml.safe_load(...)` を素朴に使い、libyaml backed の `yaml.CSafeLoader` に切り替えるだけで
  5 倍速くなる事実を見落とした
- YAML loaderの高速化が個別moduleに閉じ、他のYAML readerがpure-Python loaderへ戻った

### 発生理由

- `yaml.safe_load` は安全だが、デフォルトで pure-Python loader を使う。`yaml.CSafeLoader` の
  存在を明示しなければ libyaml の C 実装は呼ばれない
- hot path の判定を勘で行い、cProfile を取らずに「ロジックの 1 pass 化」「並列化」など
  micro-optimization を先に検討してしまう

### Commit前に止める条件

- [ ] **YAML 読み込みは必ず `from baibai_engine.foundation.yaml_io import safe_load` 経由**で書く。
      `yaml.safe_load(...)` / `yaml.load(...)` を直接呼ぶ src コードは書かない
- [ ] 新規 runtime モジュールで YAML 読み込みを足すときは `yaml_io.safe_load` が import されているか
      確認する。`rg "yaml\\.safe_load" engine/src web/backend/src batch/src` は常に zero を保つ
- [ ] perf 候補を挙げる前に **cProfile で実 hot path を確定**する。
      `python -c "import cProfile; cProfile.run('...')` で cumulative time を取り、
      改善対象が cumtime の何 % か数字で示す
- [ ] perf 改善は **before/after で wall time を 5 runs 計測**し、stdev の 3σ を超える
      改善のみ「意味あり」として PR に取り込む。±1% は noise として defer

### `yaml.dump` 側

`yaml.dump` / `yaml.safe_dump` 側の hot path も同様に `yaml.CSafeDumper` を使えば加速できるが、
write side は read side ほど呼ばれないため P2 の改善候補 (cli/query.py 等)。

## 11. AP-11: 外部データの公表ラグを設計に含めないハード必須検査

### 異なる失敗類型の代表例

- multpl の月次履歴表で「当月 1 日」の行を無条件に必須とし、multpl が当月行を月の途中で追加する
  ため、毎月 1〜14 日ごろの daily batch が `us.sp500_cape` / `us.sp500_earnings_yield` /
  `us.sp500_pe` の 3 系列で必ず失敗した (#795)
- `jp.cpi.*` の staleness 境界を monthly default (`publication_lag_days` + `staleness_margin_days`)
  で解決し、e-Stat の実掲載日 (観測月 + 53〜61 日) を超えたため、次の公表を待っている平常時が
  毎月 `stale: true` になった (#594)
- どちらも取得経路そのものは正常で、壊れているのは「その期の行が在るはず」という前提だけ。
  失敗は月初・週初・公表日前へ周期的に集中する

### 発生理由

- **完全性検査と鮮度検査の混同**。完全性 (履歴に穴が無いこと) は公表済みの過去期に対してだけ
  確定でき、最新期が在るかどうかは公表スケジュールの関数である。両者を 1 つの必須検査へ畳むと、
  正常な公表ラグが障害として誤検出される
- 検査を書く時点の today が月中・公表後にあり、公表前の日付でその検査を通したことが一度もない
- 「必須にするほど厳しく検査するほど安全」という直観。最新期を必須にした瞬間、**正しいデータが
  周期的に拒否される**という向きの逆転が起きるため、レビューでも「完全性検査は正しい」と見えて
  素通りする
- 周期的な失敗は恒常赤として定着し、「赤 = 見に行く」を壊して本物の障害を埋もれさせる

### Commit前に止める条件

- [ ] 定期公表 series の必須範囲を「公表済みであることが保証できる期」までに閉じているか。
      当該期は次のどちらかで扱う:
  - [ ] 実在するときだけ検査対象にする (`macro/indicators/providers/multpl.py` の
        `_required_latest_month`)
  - [ ] 公表締切日を持ち、締切前は要求期を 1 つ手前へずらす
        (`macro/indicators/providers/tsr_bankruptcies.py` の `_PUBLICATION_DEADLINE_DAY`)
- [ ] 鮮度の劣化を必須検査でなく staleness 判定側で検出しているか。staleness の境界は generic
      default でなく、その series の実公表暦を一次情報 (公表機関の release schedule / 実掲載日)
      で確認した値になっているか
- [ ] 検査対象期間の端に、データが存在すると保証できない日 (未公表期・非取引日・休場日) を
      置いていないか
- [ ] 日次 job がその provider を呼ぶ窓 (`--start` / `--end`) を月初・週初・公表前日について
      書き出し、必須範囲がその全ての日で満たせることを確認したか
- [ ] 新しい完全性検査を足したら、**公表直前の日付を today に固定した negative test** で正しい
      データが拒否されないことを確認したか。判定に使う today は引数で注入し、実装内部から
      現在時刻を直接読まない (`_required_latest_month(*, end, today, available)` が型見本)

## 12. AP-12: 量の基準を確かめずに組み合わせる

### 異なる失敗類型の代表例

- 年間 DPS を分割 factor 1 つで asof 基準へ換算した。DPS は支払ごとの基準日の株式基準で
  書かれるため、会計期間が分割を跨いだ年度は 1 つの係数で換算できず、利回りが 86.6% になった
- accruals の純利益を `1 株当たり当期純利益 x 発行済株式数` で再構成した。EPS の分母は期中平均
  株式数、`ShOutFY` は自己株式込みなので、報告値と 1% 以内で一致するのは 32.4% だけだった
- TTM を `直近累計 + 前期通期 - 前年同期間累計` で 1 株当たりのまま合成した。株数が動いた会社
  では和・差が成立せず、黒字の会社が赤字に見えた (121,439 断面で符号反転 104 件)
- 予想 DPS を時間制限なしで遡り、無配化した会社に取り下げ前の予想の利回り 13.46%/年 が付いた
- EDINET の書類を単体基準で読んだ値を、連結の時価総額と組み合わせた。7203 は親会社単独の
  貸借対照表から「時価総額の 12.41% が純現金」という値が判断面に出ていた
- 発行済株式総数と自己株式数を別々の最新行から carry し、自己株式の消却後にも消却前の
  自己株式数を再控除した。9441 では実際の発行済 12,240,712 株から消却済み 7,957,088 株を
  引き、時価総額の株数を 65.0% 過小にした
- 正の自己株式の後に `ShOutFY` は更新されたが `TrShFY` が空欄の行で、古い自己株式をcarryした。
  空欄からは0株・減少・未報告を区別できず、6184と7049で旧値が現在も有効でない場合は、
  時価総額をそれぞれ最大4.8%、7.9%過小にする
- EPS の期中平均株式数 `AvgSh` を gross issued の fallback として保存し、正の自己株式数を
  もう一度控除した。4167 では 7,563,857 株から 352,373 株を控除したが、gross issued は
  7,916,230 株だった
- `total_assets` と `EqAR` を別々の最新行から carry して掛け合わせた。9628 では 2026-05-15
  の総資産と 2026-02-13 の自己資本比率を組み、同一行の組より自己資本を 28.6% 過大にして
  PBR を 22.2% 過小にした
- corporate-action eventをclose付きprice barとして読み、売買停止で`close = NULL`の権利落ち行を
  捨てた。6731の100株→1株の併合が価格・株数・forward returnの全経路から消え、時価総額を100倍、
  60日price returnを`87.5`（+8,750%、価格比88.5倍）として判断面へ出した
- 財務履歴の下限を最古の行 (`MIN(disclosed_at)`) から取った。読み取りの可否を決めるのは
  coverage であり、10 年の移動窓が通り過ぎた 1,911 行が下限を coverage の外へ引き下げて、
  最近の履歴しか要らない cohort まで含め 80 cohort 全部が構築不能になった
- 「日米独の実質割引率」として、米の実質 10 年 2.43% と日独の**名目** 10 年 2.815% / 2.97% を
  並べた。real と nominal は同じ「10 年金利」の語で並ぶので、名前では衝突しない
  (macro context 2026-08-12)
- 実質賃金指数の deflator (持家の帰属家賃を除く総合) と `jp.cpi.core_yoy` を同じ量として結び、
  両者の関係を会計恒等として書いた。**0.5pt の許容幅を置いたこと自体が恒等でないことを示していた**

### 発生理由

- 量が持つ基準 (**資本基準 / 株式基準 / 実体 / 期間 / 観測の齢**) が field 名にも型にも現れず、
  組み合わせる場所で誰も一致を確かめない
- マクロ系列は名前が近いほど基準が違う (「10 年金利」は real / nominal、「コア CPI」は対象範囲が
  publisher ごとに違う)。基準は `macro reading` の `statistic` / `statistic_unit` / `unit` に出て
  いるのに、prose へ引用する段で落ちる
- 誤りは型を通り、値は有限で、単体テストは緑のまま通る。**fixture 自体が恒等式を破っていても
  誰も気付かない**
- 「per-share の値を足す」「per-share に株数を掛ける」が書けてしまう
- source 側の語 (`ShOutFY` は自己株込み、`eps_ttm` は期中累計) を名前どおりに読む
- 1 つの状態を構成する field を独立に carry し、途中の消却・発行で同じ状態を指さなくなった
  ことを検査しない
- source alias の値型だけを合わせ、期末 gross issued と期中平均 ex-treasury の会計概念を
  同じ field へ入れる

### Commit前に止める条件

- [ ] 2 つの量を比・差・積にする前に、両方の **資本基準・株式基準・実体・期間** を書き出したか
- [ ] **per-share 値の和・差を作っていないか。** 合成は円で行い、1 株当たりへの換算は最後に
      1 回だけ行う。per-share 同士の比 (YoY) は正しい
- [ ] **per-share 値に株数を掛けて総額を作っていないか。** 総額が要るなら報告された総額の行を読む
- [ ] その基準の一致を、**store が既に持つ独立な冗長性**で確かめたか。同じ量を別経路で出す値が
      あるはずで、実データで次が成り立つ (`reference/valuation-metrics.md` §5.1 が正本)
  - [ ] 開示された自己資本比率 == `bps` x 自己株控除後株数 / `total_assets`
  - [ ] 報告 `profit` == `eps_ttm` x `average_shares`
  - [ ] EDINET の `total_assets` == 短信の `total_assets`
- [ ] 追加した fixture が上の恒等式を満たすか。**破っている fixture は期待値ごと誤りを保存する**
- [ ] 予想・実績を混ぜる経路で、**古い観測が新しい観測を上書きしていないか**
- [ ] 同一会計期間の部分訂正で、訂正行に無いactual fieldを欠損へ戻していないか。営業利益、
      経常利益、純利益のfallbackは、選択した期間内の具体的なfieldを優先し、forecastの空欄を
      actualの部分訂正と同じfallback規則にしていないか
- [ ] 1 つの状態を構成する複数 field を別々の行から carry する場合、途中の消却・発行・分割を
      跨いでも同じ状態として両立することを検査したか。正の自己株式を観測した後の新しい
      `ShOutFY`行で`TrShFY`が空欄なら、発行済の増減・不変にかかわらず古い自己株式を引かないか。
      新しい`TrShFY`を観測するまで古いbasisを復活させていないか。別開示日の`TA × EqAR`を
      自己資本としていないか。同一行へ揃えた組が最新`BPS`より古いとき、古い資本を復活させて
      いないか
- [ ] source alias は値の形ではなく会計概念で束縛したか。`AvgSh` を `ShOutFY` の欠損補完に
      使っていないか。既存cacheを守るなら、単なるfield同値ではなく`AvgSh + TrShFY`が過去の
      gross issuedへ戻る隣接恒等式まで確認し、正常な1Q行を除外しないか
- [ ] 株式数の値域を合成前に検査したか。負の自己株式、非正の発行済、発行済以上の自己株を
      差し引いて正の値へ見せていないか。carry後の現在値だけでなく、正の自己株を観測した
      source行自体の発行済との関係も検査したか。自己株0株を同じ理由で過剰除外しないか
- [ ] carry の互換性判定を外す mutation と、period-average alias を戻す mutationの双方で
      negative test が失敗するか。issuedの増加・減少・不変と、明示的な自己株式0株、新しい正の
      自己株式観測を分けて固定したか
- [ ] 「値を知らない」ことを 0 や False で表していないか。carry のような和では、値を出さないこと
      が下流で「0 である」という主張になる
- [ ] 計測スクリプトで検証する場合、**pipeline を再実装せず実装の関数をそのまま呼んだか。**
      再実装した計測は再実装を測る (分割正規化の再現漏れで 3 回続けて誤った結論を出した)
- [ ] **field 名を信じる前に定義を読んだか。** source の語をそのまま持つ field は名前が嘘をつく —
      `eps_ttm` は期中累計であって TTM でなく、`shares_outstanding` は自己株式を含む。
      基準は型で強制できないので、名前は最後の防御線にならない
- [ ] **store が「持っている」ことと「出せる」ことを同じ量として扱っていないか。** 読み取り範囲の
      端は行から導かず、その範囲を出せると宣言している側から取る。source の窓は動くので、
      取得できた事実は保持し続ける保証にならない
- [ ] マクロ系列を並べる前に、**real / nominal・stock / flow・水準 / 前年比・観測 / 期待**が全系列
      で揃っているか。`macro reading` の `statistic` (`level` / `yoy`)・`statistic_unit`・`unit` は
      系列ごとに違う
- [ ] 2 つの指数を恒等式で結ぶ前に、**deflator・基準年・対象範囲**が同じ定義かを一次資料で確認
      したか。**許容幅を置きたくなったら、それは恒等ではない** — 関係の性質を identity から
      経験的関係へ落とす

## 13. AP-13: 変動が構造的に存在しない値を、変動する値として読む

### 異なる失敗類型の代表例

- `adjustment_factor_coverage` は store の 10,132,436 本すべてで `complete`。総リターンと公開買付け
  価格を守る 2 か所の判定と authority gate がこの値を読むが、**拒否側が一度も観測されていない**ため、
  働くことが示されていなかった
- `is_common_stock` は master の 568,329 行すべてで 1。証券種別の field を source が返さないので、
  判定関数が入力欠落で `True` へ fail-open していた。instrument type の除外は一度も発火せず、
  診断 `exclusion_counts["non_common_stock"]` は常に 0 で「弾いた」と読めた。適格市場区分に ETF と
  優先出資証券が残り、片方は 80 cohort すべてで screen を 28 回通過していた
- `price_to_equity`はcash-richの第2整列キーだが、**どのEvidence Patternも書き込まない**。全候補で既定値
  99.0 に落ち、同点は ticker 順へ抜けていた。銘柄横断の順位キーなのに順位を付けていない
- 業種中央値は母数 10 未満で市場中央値へ落ちるが、落ちた事実がどこにも残らない。同じ field が
  「業種との差」と「市場との差」の 2 つの量を指し、(asof, sector) の 26.4% で後者だった
- corporate-action eventをprice barの一種として読み、`close = NULL`の56 eventを構造的に
  消していた。factorは存在するのに価格が無いという正規の状態がreaderの出力型に無かった

### 発生理由

- **型は通り、値は有限で、テストは緑のまま。** どれも例外を出さず、欠損にもならない。コードを読んでも
  「変動しうる値を正しく扱っている」ようにしか見えない
- 防御が想定する失敗の形と、実際に起きる失敗の形がずれている。自己株買付の残枠は「読めなかったら欠損に
  する」規約で守られているが、実際の故障は**読めていて間違っている**形なので guard が作動しない
- 通過側だけが観測される検査は、通過を確認しても働くことを確認したことにならない
- source の形は世代で動く。field が消えても、fail-open した判定は静かに答えを返し続ける

### Commit前に止める条件

- [ ] その値は**実際に 2 通り以上の値を取ったことがあるか。** 全期間の store で `COUNT(DISTINCT)` を
      取る。1 なら、契約検査なのか、届かない分岐なのかを判別してから残すか消すかを決める
- [ ] 拒否・除外・fallback の各分岐に**負側テストがあるか。** 誤入力を与えて実際に落ちることを確認する。
      通過側だけのテストは guard を検証していない
- [ ] 診断カウンタが常に 0 のとき、「対象が無い」と「検出できない」のどちらかを確認したか
- [ ] 読む側が存在する key を、書く側が実際に書いているか。`grep` で読み書き両方を数える
- [ ] fallback した事実を、値と同じ粒度で残しているか。素性の無い fallback は、後から効果を分けられない
- [ ] source が答えられないことを、答えが真であることと区別しているか。field の欠落で `True` を返す
      判定は、source の形が変わった瞬間に静かに壊れる
- [ ] eventの存在を、同じ日の価格・出来高など別のoptional値の存在で判定していないか。
      event-only型とprice型を分け、`close = NULL`のevent fixtureを全readerと鏡像consumerへ通したか

### 一括検出

個別に探すより、**store の全列に「2 通り以上の値を取ったか」を問う方が速い**。calibration の
panel 70 列・forward 16 列と、local SQLite 4 store の全 table を 1 回走査して、上の 4 件のうち
3 件と、他 5 件の候補（`source_coverage.status` が常に `ok`、`capex_source` が 1 値、
`macro.series.priority` が 1 値など）を検出した。

## 14. AP-14: 実装が追い越した記述を、追い越された日に直さない

### 異なる失敗類型の代表例

- `architecture.md` が lineage の retained kind として `l1_release` を挙げた直後に「L1 release は
  この union に入れない。closure resolver が揃うまで kind を戻さない」と書いていた。resolver は
  実装済みで、`CohortSourceRef` は `l1_release` を含む。**同じ文書の中で矛盾していた**
- calibrationがlake外の`source_coverage`も読むのに、L1 factの一致だけで全入力を
  `rebuildable_input`と記述していた。保持対象とconsumer入力の棚卸しが同時に更新されていなかった
- lake の table 数が 15/4 → 17/2 へ動いた変更で、同じ file の 3 箇所だけが直り 5 箇所が残った。
  結果として 1 つの file の中に 17 と 15 が併存した
- `OPERATIONS.md` が serving views を「store push と同時に走らせる」と書き続けていた。workflow は
  machine push が成功した場合にしか views を走らせない
- L1 export の実測が、doc 自身の失効条件（実装 digest を 5 file から取る）を満たしたまま残った。
  digest は動いていて、記録は期限切れだった
- 較正 store の全再構築の所要が reference・skill・実測で 3 通りに分かれていた

### 発生理由

- **「将来こうなる」は書いた時点で正しいので、review で誤りとして見えない。**誤りになるのは後日で、
  そのとき誰もその段落を読み返さない
- 変更 PR は自分が触った file を直すが、同じ契約を**別の言葉で述べている file** を探さない。
  file 名では見つからず、主張の語（「union」「同時」「本」「戻る」）でしか引けない
- 数値は「いつ信じてはいけないか」を併記しないと、古くなったことが誰にも観測できない
- 同じ事実が 2 か所以上にあると、片方だけが直る。正本を決めていないと、どちらが古いか判らない

### Commit前に止める条件

- [ ] 実装の前提・境界・数値を変えたら、その契約を述べている file を**主張の語**で `rg` する。
      触った file の中も端から端まで見る（同じ file の中に古い値が残るのが最頻）
- [ ] 「〜まで」「〜時点で」「〜になったら」を書くなら、**その条件が満たされたときに何を直すか**を
      同じ段落に書く。書けないならその条件は書かない
- [ ] 計測値を doc へ置くときは失効条件を併記し、条件は機械で判定できる形にする
      （実装 digest・schema version・行数）。失効した値は消すのではなく**取り直す**
- [ ] 同じ数値・同じ契約が複数 doc にあるなら 1 つを正本にし、他は参照にする
- [ ] doc の契約を直したら、同じ契約が**コードの comment / docstring** にも書かれていないか確認する
      （`margin_*` の source 規則は reference と `calibration/panel.py` の両方にあった）
- [ ] 手順の正本を書いたら、その障害の**入口から辿れるか**を確認する。runbook を書いても運用 skill
      から link が無ければ、operator は届かない

### 機械検査を置かない理由

doc の主張を一般に機械照合することはできない。機械化できる下位ケース（実装 digest に結んだ計測値）に
gate を置くと、`models.py` のような日常的に触る file を変更するたびに数分の再計測を要求することに
なり、発生頻度に対して釣り合わない。ここは検査でなくチェックリストで持つ。

## 15. 正本・test・reference

各`AP-*`は確認観点だけを所有する。次の表は、厳密な契約へ到達するための代表的な入口である。
値、field、処理、停止挙動は、リンク先が示すdomain modelと実装testを正本とする。

| Anti-pattern | Owner / reference | 主な機械確認 |
| --- | --- | --- |
| AP-01 | [`reference/judgment-writing.md`](./reference/judgment-writing.md)、[`reference/data-sources.md`](./reference/data-sources.md) | source検証とartifact validator |
| AP-02 | [`reference/thesis.md`](./reference/thesis.md)、[`portfolio-management.md`](./portfolio-management.md) | model testと数式の再計算 |
| AP-03 | [`reference/valuation-metrics.md`](./reference/valuation-metrics.md)、[`reference/estimate-calibration.md`](./reference/estimate-calibration.md)、[`reference/portfolio-ledger.md`](./reference/portfolio-ledger.md) | adjustment・return・outcomeのcontract test |
| AP-04 | [`architecture.md`](./architecture.md)と各domain model | model・CLI contract test |
| AP-05 | [`doctrine.md#fact-analysis-separation`](./doctrine.md#fact-analysis-separation) | machine storeのschemaとvalidator |
| AP-06 | [`reference/macro.md`](./reference/macro.md) | macro contextのmodel・source test |
| AP-07 | [`reference/data-sources.md`](./reference/data-sources.md) | schedule・freshness test |
| AP-08 | 各domain modelと対応するnegative test | [`tools/quality/drift/`](../tools/quality/drift/)とdomain test |
| AP-09 | [`reference/thesis.md`](./reference/thesis.md)、[`reference/portfolio-ledger.md`](./reference/portfolio-ledger.md) | research・assessment・ledger contract test |
| AP-10 | `baibai_engine.foundation.yaml_io` | import checkとprofile実測 |
| AP-11 | [`reference/data-sources.md`](./reference/data-sources.md) | 公表前・公表後の境界test |
| AP-12 | [`portfolio-management.md`](./portfolio-management.md)、[`reference/thesis.md`](./reference/thesis.md)、[`reference/valuation-metrics.md`](./reference/valuation-metrics.md)、[`reference/macro.md`](./reference/macro.md) | unit・basis・cross-field test |
| AP-13 | 各sourceのregistryとreader contract | 時系列変化とsource更新のtest |
| AP-14 | [`README.md#document-writing-contract`](./README.md#document-writing-contract) | link・CLI・reference・literal drift gate |

AI agentの作業規約と本checklistへの参照経路は[`../AGENTS.md`](../AGENTS.md)が所有する。
