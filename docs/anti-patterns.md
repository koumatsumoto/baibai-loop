# anti-patterns

baibai-loop での AI agent 作業で観測された失敗パターン集と、再発防止のためのチェックリスト。
PR で繰り返し指摘される類型は本ドキュメントに集約し、self-review の strict gate として運用する。

このドキュメントは事後分析のためではなく **作業前 / commit 前 / PR 前のチェックリスト** として
読まれることを意図する。新規 brief / outlook / research を書く前に必ず該当節を読み返すこと。

## 0. 全 anti-pattern 共通の根本原因

PR #68 (2026-05-04 outlook + 6590 research) で 2 ラウンドのレビューで合計 21 件の指摘を
受けた。共通する根本原因は以下:

1. **一次情報を確認せずに二次情報・推測で書く**
2. **数値を機械的に検算しないまま記述する**
3. **データの「異常さ」に対して原因 cross-check を skip する**
4. **schema / 実装の意味を読まずに「だろう」で書く**
5. **fact 層と分析層の境界を曖昧にする**
6. **依存関係 (brief → outlook → research) の整合性を意識しない**
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
- OPEC+ 5/3 statement を outlook / brief 作成日 (5/4) に確認していなかった

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
- [ ] outlook / brief の発行日付近に大型 statement (FOMC / BOJ / OPEC+ / CPI / PCE) が予定
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

- [ ] candidates 由来の `price_change_60d` / `price_change_4w` が **±50% を超える銘柄**は、
      research に進める前に以下を確認:
  - [ ] EDINET の臨時報告書・有価証券届出書で 60 日 / 4 週 期間内の corporate action
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
- candidates / outlook YAML schema の追加プロパティ可否を確認せず `note` / `previous_change`
  を勝手に追加 → validate error
- outlook YAML schema の `source_refs` が brief YAML パスに限定されることを確認せず
  research-log.md を指定 → validate error

### 根本原因
- field 名から意味を「だろう」で推測する
- schema JSON / 実装コードを読み直さない
- 既存サンプルとの diff を意識しない

### 再発防止チェックリスト

- [ ] candidates / outlook / brief / research の field を新規に解釈・記述する前に、対応する
      JSON schema (`records/_schemas/*.json`) を読み返したか
- [ ] 計算系 field (percentile / rank / change / hit) は src 実装 (`src/baibai_loop/screening/`)
      で計算ロジックを確認したか
- [ ] 既存ファイル (4/24 candidates、4/24 bootstrap outlook、4/25 research) のサンプル形式に
      従っているか、独自構造を勝手に追加していないか
- [ ] `additionalProperties: false` の object に独自 key を追加していないか

## 5. AP-05: fact 層と分析層の境界を曖昧にする

### 観測された症状
- brief の `note` / `fact_memos` / `events` に「FOMC タカ派ホールドの正当化材料」「需要側
  冷却の early signal」「油価高値圏粘着の構造要因」「122 条効果が顕在化」などの解釈・因果
  推論・意味付け表現を書いた (docs/design-principles.md §4.3 で禁止)

### 根本原因
- brief = 事実層 / outlook = 分析層 の境界を意識せず、便利な要約として書く
- design-principles.md §4.3 の禁止表現リスト (「示唆」「背景」「受けて」「意味する」) を
  読み返さない

### 再発防止チェックリスト

- [ ] brief の地の文に以下の表現が含まれていないか:
  - [ ] 「示唆する」「観測される」「受けて」「背景に」「意味する」
  - [ ] 「正当化材料」「early signal」「顕在化」「構造要因」
  - [ ] 「注目すべき」「重要な」「焦点となる」 (Major/Notable は閾値ラベルでありこの意味では
        使わない)
- [ ] brief は `数値 + 公表日 + 機械的前期比 + source URL` のみで構成されているか
- [ ] 解釈・因果推論・予測は outlook の summary / rationale に移したか
- [ ] outlook で fact を引用するときは brief パスを `source_refs` で必ず参照しているか

## 6. AP-06: 依存関係 (brief → outlook → research) の整合性を skip する

### 観測された症状
- outlook が米コア PCE +3.2% を引用するが、brief (`2026-03-macro-monthly-us-cpi-3p3.yaml`) では
  `unreleased` のまま放置。fact layer を skip して analysis layer に最新値を直接入れた
- outlook の sector rationale で TSMC / NVIDIA / EIA / ホルムズ等の deep research fact を
  使用しているのに、対応する brief への `source_refs` 参照が抜けていた (35 箇所)
- emerging.source_refs が空のまま中国 PMI / 輸出 / 不動産を rationale に書いていた

### 根本原因
- 「outlook で書いてしまえば伝わる」と判断して brief 経由を skip
- source_refs の意味 (= research が macro_gate を再構成するための trace) を忘れる
- design-principles.md の柱 (事実層と分析層の物理分離、updated_from は判定根拠列挙) を
  運用で守らない

### 再発防止チェックリスト

- [ ] outlook で引用する **すべての fact** について、対応する brief YAML が存在するか
- [ ] 存在しない fact は、outlook 作成と同じ commit で **新規 brief を追加**してから引用
- [ ] outlook の各 sector / region / changes の `rationale` に出てくる fact 引用について、
      対応する brief パスが `source_refs` に含まれているか機械的に対応関係を確認
- [ ] **fact item は `status: ok` の `source_id` を少なくとも 1 つ持つこと**。`status: failed`
      / `partial` の source だけを根拠にして fact 値を入れていないか
  - `failed` source は「Tier 1 を試行したが取れなかった」記録として残してよいが、その値の
    根拠としては機能しない。値を入れるなら **同じ事実を取得できた `status: ok` の二次
    source を別 id で宣言**し、`source_ids` に併記する (例: `china-customs-toplevel:
    failed` + `tradingeconomics-cn-exports: ok` の併記、Tier 2 明示)
  - 一次が取れない期間が続くなら、`data-sources.md` 側で恒常的代替経路を Tier 1 準拠扱い
    に格上げするか、Tier 2 / 補助外運用を明示する
- [ ] research の `outlook_ref` / `brief_refs` / `candidates_ref` の 3 ref が valid パス
      かつ実在するか
- [ ] `updated_from` は「全 brief」ではなく「判定に効いた canonical input 集」であることを
      意識して列挙しているか
- [ ] outlook の正本フローを守っているか: **canonical fact layer は brief のみ**。outlook
      の `updated_from` / `source_refs` は `records/01-brief/**.yaml` のみで、外部 URL を
      直接書かない。sidecar `outlook-<date>-research-log.md` は取得ログであり source 数
      にも数えない (詳細は [`components/outlook.md`](./components/outlook.md) §9.1)

## 7. AP-07: 公表日 / 期間 / source の最新性確認を skip する

### 観測された症状
- 米 4 月 PCE 公表予定を「5/30 前後」と書いた (BEA schedule で確認した正確な日付は
  2026-05-28 8:30 EDT)
- outlook 5/4 公開時に OPEC+ 5/3 statement を反映しなかった
- next_events に source_ids を紐付けず、BLS schedule などの一次情報を素通り

### 根本原因
- 「だいたいの日付」感覚で next_events に書いてしまう
- 発行直前の重要 release (前日・当日) を「まだ早すぎる」と勝手に判断して skip
- source_ids 紐付けを必須運用していない

### 再発防止チェックリスト

- [ ] outlook / brief 内の **すべての日付** について、source の publish schedule か
      release date を WebFetch で再確認したか
- [ ] outlook 発行日 ± 5 営業日に予定された FOMC / BOJ / CPI / PCE / NFP / OPEC+ のいずれかが
      あれば、最新 release / statement / minutes が出ているかを必ず確認
- [ ] `next_events` の各 entry に対応する `source_ids` を必ず紐付け (BLS schedule、BEA
      schedule、Fed FOMC calendar、BOJ schedule)
- [ ] 「随時」「○月下旬」「前後」のような曖昧表現を避け、確認できた具体日付を書く

## 8. AP-08: schema validator の抜け道を意識しない

### 観測された症状
- `adv_participation_pct: 0.585` (100 倍ズレ) を validator が catch しなかった
- 当初の整合チェックを `avg_turnover_oku` 不在時には silently skip するように実装、
  required field 化を忘れた → 抜け道残存
- nested の `valuation.adv_participation_pct` も整合チェック対象外だった
- `decision: skipped` の packet で `position_size_oku > 0` を要求していたため、
  hypothetical 値と実建玉値が混在

### 根本原因
- validator を「データが揃っている前提」で実装し、欠損時の挙動を「skip」にする
- corner case (skipped / pending / 0 値 / null) のテストを書かない
- ユーザ指摘で初めて抜け道に気付く

### 再発防止チェックリスト

- [ ] validator rule を追加・修正する場合、以下の corner case の test を必ず書く:
  - [ ] 関連 field が **不在** の場合 (skip / error どちらが正しいか)
  - [ ] 関連 field が **null** の場合
  - [ ] 関連 field が **0 / 負値** の場合 (decision との整合性)
  - [ ] **nested** field (例: `valuation.adv_participation_pct`) も同じ rule を適用するか
  - [ ] **既存 packet** (4/25 research 5 件など) が新 rule で breakage しないか、する場合は
        同 commit で fix する
- [ ] 以下の adv_participation 関連の具体条件を validator が catch するか、test を書いて
      確認する:
  - [ ] `avg_turnover_oku <= 0` は error (整合チェックの分母が成立しない、required な数値
        だけでは抜け道になる)
  - [ ] `position_size_oku == 0` の場合は **`adv_participation_pct == 0`** を要求 (skipped
        packet で hypothetical 値と取り違えると `position_size 0 / avg_turnover 85.4 *
        100 = 0` だが `adv: 1.0` のような非ゼロを期待値 0 で skip してしまう穴を塞ぐ)
  - [ ] `valuation.adv_participation_pct` (nested) も top-level と同じ整合チェックの対象
        にする
- [ ] cross-field consistency rule は **依存先の field が「数値であること」だけでなく、
      「正値 (> 0) であること」を確認**する。0 / 負値で silently skip する実装は穴になる
- [ ] 整合チェック (cross-field consistency) は片方の欠損で skip しないよう、依存 field を
      required 化する

## 9. PR review で繰り返し指摘される類型の追跡

PR で同じ anti-pattern が 2 ラウンド以上指摘されたら、本ドキュメントの該当節を強化または
新節として追加する。直近の事例:

| PR | round | 主な anti-pattern |
| --- | --- | --- |
| #68 | 1 | AP-01 (122 条 13%、TSMC/Samsung/Kioxia 断定)、AP-02 (adv 100 倍、利確 +118% / +30% 矛盾)、AP-03 (6590 split artifact)、AP-04 (sector_relative_strength_percentile 誤読)、AP-06 (米コア PCE brief 未反映)、AP-07 (PCE 5/30 前後)、AP-08 (adv consistency 抜け道) |
| #68 | 2 | AP-01 (TSMC Capex / Sovereign AI 未確認のまま outlook で断定継続)、AP-05 (brief への分析混入)、AP-06 (outlook source_refs と brief 不整合 35 箇所)、AP-07 (OPEC+ 5/3 反映漏れ、PCE 5/28 ではなく 5/30) |

## 10. 関連ドキュメント

- 思想・基本方針: [`philosophy.md`](./philosophy.md)
- 事実 / 分析の分離: [`design-principles.md`](./design-principles.md) §4
- brief 仕様: [`components/brief.md`](./components/brief.md)
- outlook 品質基準と self-review: [`components/outlook.md`](./components/outlook.md) §9
- research 採用判定: [`components/research.md`](./components/research.md)
- AI agent 規約 (本ドキュメントの参照経路): [`../AGENTS.md`](../AGENTS.md)
