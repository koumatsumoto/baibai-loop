# components/outlook.md

Baibai-Loop 4 成分アーキテクチャの **(c) マクロ見解** の運用仕様。brief を積み上げて作成されるマクロ見解で、`records/04-research/` の Macro gate 判定の唯一の source となる。全体構造は [`../architecture.md`](../architecture.md) を参照。

## 1. 役割

- **canonical fact layer は `records/01-brief/` のみ**。一次情報 (Tier 1 / Tier 2) は brief の `sources` に集約し、outlook の `updated_from` / `source_refs` は `records/01-brief/**.yaml` のみを参照する (schema で強制)。outlook 作成時の deep research transcript は sidecar `outlook-<date>-research-log.md` に保存するが、これは取得ログ専用で outlook の根拠 source 数には数えない (= sidecar だけで brief を skip するのは設計違反)
- 上記 fact layer を入力に **業種/地域/資産クラス別の追い風 (tailwind) / 中立 (neutral) / 逆風 (headwind) 評価** を生成
- `records/04-research/` の Macro gate 判定で参照される唯一の source
- Macro track の出力として、Micro track の research 選定に影響する
- **outlook は投資戦略の最上位 gate**。ここの分析の質が portfolio パフォーマンスを支配する。深さを犠牲にして時間を惜しんではならない (深い分析を要求される非常に重要なドキュメント)

## 2. 更新 trigger と頻度

### 2.1 定期

- **月次 1 回**（月初 3 営業日以内）
- 直近 1 か月の brief（週次 + 月次を基本、必要に応じて daily / event を追加）を合成して更新
- 標準の `published_at` は、必要な `world-daily` / `event` を取り込んだ **翌営業日朝（JST 06:00-10:00）** とする。同日中に出すのは緊急更新時のみ

### 2.2 不定期

以下の場合は即座に更新:

- BOJ 金融政策決定会合で決定内容があった場合（利上げ・利下げ・YCC 調整等）
- FOMC 会合で決定内容があった場合
- CPI 大振れ（予想対比 ±0.5% 以上乖離）
- 主要指数 ±5% 以上変動（Nikkei 225 / S&P 500）
- 重大地政学 shock 後

## 3. 初回作成手順

`records/02-outlook/` がまだ存在しない時点では、`records/04-research/` を作る前に以下の手順で初版 outlook を用意する。

1. 当日時点で利用可能な `records/01-brief/` を読む。**最新 brief が 5 営業日以上古い場合は `world-daily` または `event` を先に追加して freshness gap を埋める**
2. `records/02-outlook/YYYY/MM/outlook-YYYY-MM-DD-<slug>.yaml` を作成する
3. `updated_from` には、**実際に判定根拠として使った brief YAML を列挙** する
4. `sectors` は東証 33 業種を全件必須、`regions` は 4 地域を全件必須で埋める。判定材料が不足する場合は `status: null` + `rationale` で明示する（省略は不可）
5. `horizon: "1-6m"` で 1-6 か月先の見通しを記述

初版段階では保守的に neutral を多くする（headwind 判定は research 採用不可を招くため、情報不足では保守的に）。

## 4. Path と命名

```
records/02-outlook/YYYY/MM/outlook-YYYY-MM-DD-<slug>.yaml
```

- `<slug>`: 内容を示す英小文字ハイフン区切り（例: `q2-outlook`, `post-boj-april`, `cpi-3p3-reaction`）

## 5. YAML 必須項目

```yaml
schema_version: 1
ai_draft: true | false
published_at: "ISO 8601"
horizon: "1-6m"                     # 想定先読み期間
updated_from:                       # この outlook を作る元になった brief YAML
  - records/01-brief/YYYY/MM/...yaml
summary: <PART A-E (構造分析 / 4 シナリオ / リスク 10+ / 投資方向性 / 次回 trigger) を含む multi-paragraph long-form。詳細は §9.2 参照>
sectors:                            # 東証 33 業種を全件必須
  "水産・農林業":
    status: tailwind | neutral | headwind | null
    rationale: <判定根拠>
    source_refs: [records/01-brief/.../*.yaml]
  # ... 33 業種を全件記入する
regions:                            # 4 地域を全件必須
  us:                    { status: ..., rationale: ..., source_refs: [...] }
  japan-domestic:        { status: ..., rationale: ..., source_refs: [...] }
  japan-external-demand: { status: ..., rationale: ..., source_refs: [...] }
  emerging:              { status: ..., rationale: ..., source_refs: [...] }
changes:                            # 前回 outlook からの判定変更
  - target: <sector or region>
    from_status: ...
    to_status: ...
    rationale: ...
    source_refs: [...]
next_triggers:
  - date: "YYYY-MM-DD"
    text: "<次回更新 trigger となるイベント>"
```

- `ai_draft`: AI 下書き段階では `true`、人間確認後 `false`
- `sectors` のキーは東証 33 業種の正式名称を全件使う（[`../screening/valuation-metrics.md`](../screening/valuation-metrics.md) 参照）。省略は不可
- `regions` は `us` / `japan-domestic` / `japan-external-demand` / `emerging` の 4 件を全件必須
- 判定できない項目は `status: null` + `rationale` で明示する（key の省略は不可）
- 詳細な schema は [`../../records/_schemas/outlook-v1.json`](../../records/_schemas/outlook-v1.json)

### 5.1 null status の扱い

`status: null` とした sector / region は、research 側の Macro gate 判定では **`neutral` 扱い** とする。情報不足を理由に `headwind` 側へ倒さない（採用率が過度に下がるのを避ける）。`null` でも `rationale` は必須。outlook が充実してきたら `null` を明示的な判定に更新する。

### 5.2 `updated_from` の選び方

- `updated_from` は「存在する brief の全列挙」ではなく、**今回の判定に効いた canonical input 集** を書く
- 通常更新では、**前回 outlook 以降に追加された brief すべて + 前回 outlook の tailwind/headwind 判定を支えた brief の最新版** を入れる

### 5.3 sector / region の責務分離

- 同一マクロ根拠を `sectors` と `regions` の両方に重ねて tailwind/headwind 化しない
- 円安、外需、米最終需要のような **横断的要因** は `regions.japan-external-demand` などの地域軸へ寄せる
- `sectors` に tailwind/headwind を付けるのは、その業種固有の追加根拠がある場合に限る

### 5.4 schema 検証

outlook YAML の構造、必須キー、`sectors` の 33 業種完全性、`regions` の 4 地域完全性、`status` 許容値、`updated_from` / `source_refs` の `.yaml` 末尾は `baibai-loop-validate` で検査される。CI の `Validate artefacts` step で merge gate になる。手元では `uv run baibai-loop-validate --target outlook` で個別に走らせられる。

## 6. フィールドの書き方

- outlook は **分析層**（philosophy 柱 1）。解釈を書いてよい
- ただし、根拠となる brief への参照を必ず付ける（`source_refs` / `updated_from`）
- `summary` は PART A-E (§9.2) を含む long-form の multi-paragraph で現在のマクロ見解を構造的に記述する。1 段落の要約では深さが足りず investor behaviour を支配できないため不可
- 各 sector / region の `rationale` は判定根拠を 1〜2 文で記述する
- `changes` には前回 outlook からの判定変更を `target` / `from_status` / `to_status` / `rationale` で構造化する
- `next_triggers` は次に outlook を更新すべきイベントを列挙
- 投資判断の示唆は軽く（「このマクロ下では... が相対的に有利」程度）、個別銘柄への言及はしない（それは research の仕事）

## 7. research への接続

### 7.1 Macro gate 判定

`records/04-research/` の front matter `macro_gate` は、この outlook の `sectors` / `regions` を参照して決まる:

- 対象銘柄の属する業種・地域の outlook 判定を取得
- 業種と地域で判定が食い違う場合は **保守的な方を採用**（headwind >> neutral >> tailwind）
- 詳細: [`../screening/macro-gate-procedure.md`](../screening/macro-gate-procedure.md)

### 7.2 outlook 未更新時

最新の outlook が古く、その後に重大 brief が出て gate 判定に影響する場合:

- 該当 brief を research の `brief_refs` に追加
- gate 判定を **保守側にのみ** 手動上書き可（tailwind → neutral、neutral → headwind。逆向きの上書き不可）

常態的に outlook が遅れるようなら、outlook の更新 trigger を見直す。

## 8. AI の役割境界

| 作業 | AI 可 | 人間のみ |
| --- | --- | --- |
| brief の読み込み・要点抽出 | ○ | |
| outlook 下書き生成 | ○ | |
| sectors / regions 判定の初期案 | ○ | 最終確定は人間 |
| 反対論点の列挙 | ○ | |
| **最終判定（tailwind/neutral/headwind）の確定** | | ○ |
| **判定根拠の最終確認** | | ○ |

## 9. 品質基準と self-review

### 9.1 必須の completeness 基準

outlook YAML は以下を満たさなければ `ai_draft: true → false` の確定に進めない。

- **20+ Tier 1 / Tier 1 準拠の一次情報源を直接根拠**とする。ただし正本フローは以下に厳格に従う:
  - **canonical fact layer は brief のみ**: outlook の `updated_from` および `source_refs` は `records/01-brief/**.yaml` パスのみを許容する (schema で強制)。outlook 直接の外部 URL 引用は禁止
  - **外部 deep research の取扱**: 取得した一次情報を outlook で使う場合、必ず **対応する brief (世界週次 / 日次 / 月次 / event) を同 PR で新規作成または更新**してから、outlook がその brief を `updated_from` / `source_refs` で参照する形に集約する
  - **sidecar (`outlook-<date>-research-log.md`) の役割は取得ログ**: deep research 中に確認した URL / 取得日 / Tier / key fact をリスト化し、再現性確保と監査用途で残す。outlook の根拠 source としては数えない (= research-log だけで brief を skip するのは設計違反)
  - 必要 axis (各 3-5 source、合計 20+):
    - 米マクロ axis: BLS / BEA / FRB / FOMC / Census 等
    - 地政学・エネルギー axis: EIA / IEA / OPEC 等
    - 為替・金融政策 axis: FRB / BOJ / ECB / BoE
    - 日本マクロ axis: BOJ / 財務省 / 総務省 / 内閣府 / 経産省
    - セクター動向 axis: 主要企業 IR / SEMI / IATA / Baltic Exchange 等
    - リスク資産・債券 axis: FRED / Treasury / CBOE 等
    - 中国・新興国 axis: PBOC / NBS / 海関総署 等
- **summary に PART A-E の構造を含める** (詳細は §9.2 を参照)
- **シナリオ分析 4 件**: Base / Upside / Downside / Tail。各シナリオに triggering path、確度 (合計 100%)、sector 帰結、投資方向性を記述
- **リスク因子 10+**: 順位 / 確度 / 影響度 / 観測指標 / 次の確認日 を表で記述
- **33 業種 / 4 region すべての rationale に 2 因子以上の検討痕跡** (cost / revenue / 為替 / 金利 / 業種特有 / 地政学のいずれか 2 つ以上)
- **deep research を 30 分以上実施**してから書く (general-purpose subagent 活用も可)。所要時間目安は deep research 30 分 + 構造分析 30 分 + self-review 15 分 = 計 75 分以上

### 9.2 summary の構造 (PART A-E)

`summary` は単一段落ではなく、以下の構造を持つ multi-paragraph で書く:

- **PART A. 現状の構造分析 (axes 5-6)**: 米マクロ / 地政学・エネルギー / 為替・金融政策 / 日本マクロ / リスク資産 / セクター固有テーマ
- **PART B. シナリオ分析 (4 シナリオ)**: Base / Upside / Downside / Tail、確度合計 100%、各 triggering path・sector 帰結・投資方向性
- **PART C. リスク因子の優先順位 (10+ 件)**: 順位 / リスク / 確度 / 影響度 / 観測指標 / 次の確認日
- **PART D. 1-6m 投資方向性**: 主軸 / 補助軸 / 避ける軸 / ポートフォリオ偏在管理
- **PART E. 次回 outlook 更新の trigger と reweight 軸**: 主要 economic events、利上げ会合、CPI / PCE 公表

### 9.3 self-review チェックリスト

`ai_draft: true → false` の確定前に以下をチェック:

- [ ] 20+ 一次情報源を sidecar `outlook-<date>-research-log.md` に列挙したか
- [ ] 4 シナリオ (Base / Upside / Downside / Tail) を確度付きで書いたか、合計 100% か
- [ ] 10+ リスク因子表を観測指標付きで書いたか
- [ ] 33 業種すべて rationale に 2 因子以上の検討痕跡を残したか
- [ ] 「業種固有 signal が brief 群から確認できない」一辺倒の rationale が 33 業種中 5 件以下か (5 件超なら検討不足)
- [ ] regions 4 件すべて status / rationale / source_refs を埋めているか
- [ ] FOMC / BOJ / 主要中央銀行 statement の声明文を直接引用 (内容を要約で済ませない) しているか
- [ ] 油価 / 為替 / 主要金利 の数値が brief と一致しているか
- [ ] 思い込みではなく source URL を伴う事実だけで根拠を組み立てているか
- [ ] brief 内の外交イベント (例: 「de-escalation」) と物理的フロー (例: 「ホルムズ閉鎖継続」) を区別し、deep research で cross-check したか
- [ ] 個別銘柄言及がないか (ある場合は research の責務、削除)
- [ ] `next_triggers` に主要イベント (FOMC / BOJ / CPI / PCE / 雇用) を網羅しているか

### 9.4 禁止される failure mode

過去のセッションで観測された outlook の品質低下パターン。これらが見つかれば即書き直し:

- **neutral 量産**: 33 業種すべて neutral にして「分析放棄」する状態。bootstrap の保守化原則 (§3) は判定材料が無い sector を null/neutral にする逃げ道であって、brief で確認できる事実から sector に効く因果は判定すべき
- **思い込み**: 自分の事前知識ベースで OPEC supply discipline / シェール頭打ち / 政治情勢などを「事実」として記述する。一次情報で確認しない限り「事実」と扱わない (詳細: [`../anti-patterns.md`](../anti-patterns.md) AP-01)
- **表層的 summary**: FOMC hold + BOJ hold + 円高反転と表層を並べただけで、構造分析 (なぜ油価が地政学緩和後も粘着するか、円独歩高の意味、政策金利の reaction function に油価が組み込まれた含意) が欠落
- **brief 5 件のみで作成**: 外部 deep research 無しでは 20+ source 基準を満たせない
- **fact 認識違い**: brief の「外交緩和」と「物理的フロー」を混同 (例: US-Iran de-escalation を「中東緊張緩和」と誤読し、ホルムズ閉鎖継続を見逃す)
- **brief 経由を skip して outlook で fact を直接引用**: outlook 内で fact を引用するときは必ず brief への `source_refs` を介す (詳細: [`../anti-patterns.md`](../anti-patterns.md) AP-06)
- **TSMC / NVIDIA 等の transcript 由来値を press release / earnings release 確認値として記述**: source 粒度は分けて記載し、未確認値は明示的に「参考値、要 cross-check」と書く (詳細: [`../anti-patterns.md`](../anti-patterns.md) AP-01)
- **発行日 ± 5 営業日の主要 release を見落とす**: outlook 発行直前 / 当日に FOMC / BOJ / OPEC+ / CPI / PCE / NFP の release が出ていれば必ず確認 (詳細: [`../anti-patterns.md`](../anti-patterns.md) AP-07)

### 9.5 関連: 全体的な anti-pattern 集

outlook 単体で閉じない範囲の anti-pattern (数値検算、schema 誤読、validator 抜け道、brief
への分析混入など) は [`../anti-patterns.md`](../anti-patterns.md) に集約してある。outlook を
書く前に AP-01 / AP-02 / AP-04 / AP-05 / AP-06 / AP-07 のチェックリストを 1 周すること。

## 10. 参考

- [`../philosophy.md`](../philosophy.md): 思想（マクロ優位 76/24）
- [`../architecture.md`](../architecture.md): 全体構造
- [`brief.md`](./brief.md): source となる brief の仕様
- [`research.md`](./research.md): 接続先 research の仕様
- [`../screening/macro-gate-procedure.md`](../screening/macro-gate-procedure.md): Macro gate 判定手順
- [`../templates/outlook.yaml`](../templates/outlook.yaml): template
