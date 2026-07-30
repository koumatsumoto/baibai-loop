# 3y/5y production evidence の成立条件 — 現状計測（#665）

価値tier: T1 — E[r] 実現率・selection 手法の実証改訂を止めている関門を計数で特定し、残る外部前提を人間が判断できる形にする。

改善ループ §0「現状計測の確認」にあたる観測記録。**この計測で production 変更は行わず**、rules・E[r]・selection・authority 契約の数値は変更していない。

## 0. 判定

```yaml
production_change_allowed: false
eligible_3y_cohorts: 0
eligible_5y_cohorts: 0
remaining_prerequisites: 3
blocking_reason_classes: 5
```

3y/5y evidence は 5 つの独立した理由で成立しない。3 つは同一の外部前提（取得可能履歴の窓）に帰着し、1 つは外部 source（廃止 exit value）、1 つは本 PR で直した entry 解決の欠陥（既存 cache の再構築が必要）である。**待てば成立するものは 1 つも無い。**

## 1. 再現手順

```bash
uv run baibai-engine screening calibration-evaluate \
  --calibration-dir data/screening/calibration \
  --horizon 3y --start 2022-09-30 --end 2023-06-30 --out /tmp/calib-3y.yaml
```

観測日 2026-07-30。panel grid は 46 cohort（2022-09-30〜2026-06-30、月末）。store の `cache_schema_version: 2`。

## 2. 入力の実測範囲

| store | 実データ範囲 | coverage の主張 |
| --- | --- | --- |
| `jquants_daily_bars` | 2021-08-02〜2026-07-28 | 行データから導出（DB が SSOT） |
| `jquants_fin_summaries` | 2021-08-02〜2026-07-28（90,427 行） | **2024-07-17〜2026-07-28（36,398 行）のみ** |
| `jquants_master_snapshots` | 10 日分（2026-05-13 と 2026-07-15 以降の営業日） | 日付別単日 key |

```bash
sqlite3 'file:data/screening/market.sqlite?mode=ro' \
  "SELECT MIN(disclosed_at), MAX(disclosed_at), COUNT(*) FROM jquants_fin_summaries"
sqlite3 'file:data/screening/market.sqlite?mode=ro' \
  "SELECT coverage_start, coverage_end, record_count FROM source_coverage
   WHERE source='jquants_fin_summaries'"
sqlite3 'file:data/screening/market.sqlite?mode=ro' \
  "SELECT snapshot_date, COUNT(*) FROM jquants_master_snapshots GROUP BY 1 ORDER BY 1"
```

## 3. 満期と入力窓の交差

```text
bars 非 clamp となる最小 asof = 2021-08-02 + 1200 日 = 2024-11-14
3y 満期済み asof の上限       = 2026-07-28 − 3 年     = 2023-07-28
5y 満期済み asof の上限       = 2026-07-28 − 5 年     = 2021-07-28
```

3y は「満期済み ≤ 2023-07-28」と「非 clamp ≥ 2024-11-14」が交差しない。5y の上限は bars 起点より前で、grid に存在し得ない。**現在の履歴では、満期と入力窓を同時に満たす cohort は 3y・5y ともに 0 件**である。grid 上で最初の非 clamp cohort は 2024-11-29（46 cohort 中 20 件が非 clamp）なので、非 clamp cohort が満期を迎える最短日は 3y が 2027-11-29、5y が 2029-11-29 になる。

## 4. cohort ごとの blocker（満期済み 3y = 10 cohort）

`authority_coverage.reason_counts` の実出力（blocker class → cohort 数）:

| blocker | 件数 | 帰着する前提 |
| --- | ---: | --- |
| `master_snapshot` | 10 | 履歴窓（断面 master が cohort 日に無い） |
| `input_range_clamped` | 10 | 履歴窓（bars/fin の入力窓が短い） |
| `survivorship` | 10 | 既存 panel が計測前に書かれ、件数が null |
| `priced_master_without_universe` | 10 | 同上 |
| `entry_price_gap` | 10 | 既存 forward cache（§4.1 の entry 解決の欠陥） |
| `unpriced_exit` | 5 | 外部 source（廃止 exit value） |

未解決 row の分類（本 PR で分離したもの。既存 forward cache に対する評価時導出）:

| asof | not_listed | price_gap | unpriced_exit |
| --- | ---: | ---: | ---: |
| 2022-09-30 | 308 | 64 | 1 |
| 2022-10-31 | 292 | 50 | 1 |
| 2022-11-30 | 286 | 41 | 1 |
| 2022-12-30 | 261 | 50 | 0 |
| 2023-01-31 | 260 | 62 | 0 |
| 2023-02-28 | 259 | 29 | 0 |
| 2023-03-31 | 247 | 32 | 0 |
| 2023-04-28 | 238 | 32 | 0 |
| 2023-05-31 | 238 | 53 | 3 |
| 2023-06-30 | 223 | 34 | 18 |

分離前は 270〜373 件すべてが `unresolved_forward_rows` として 1 つの blocker に畳まれていた。分解すると 3 つの異なる原因が現れる。大半（223〜308 件）は as-of 時点で市場に無かった銘柄で、投資可能でなかったので除外は正しく bias を生まない。真の survivorship 露出（廃止 exit value の欠落）は 0〜18 件。残る 29〜64 件は §4.1 の欠陥による取りこぼしである。

<a id="entry-resolution-defect"></a>

### 4.1 entry 解決が 15 日の許容を使えていなかった

`compute_forward_returns` は bar を `min(asofs)` 以降からしか読み込まないため、entry の 15 日許容が機能していなかった。asof 当日に約定しなかった銘柄（薄商いでは通常のこと）は、前営業日に値が付いていても「entry なし」と扱われる。panel は 1200 日窓から asof 以下の最終 bar を取るので価格を持っており、**panel は価格を持つのに forward は entry を持たない**という不整合が残っていた。

実例: ticker 1764 は 2021-08-02 から連続して bar を持つが 2022-09-30 の close が NULL（当日約定なし）で、panel は前営業日 2022-09-29 の 2,152 円を採っている。forward は 2022-10-03 が最初の bar になり entry なしと判定していた。

```bash
sqlite3 'file:data/screening/market.sqlite?mode=ro' \
  "SELECT traded_at, close FROM jquants_daily_bars
   WHERE ticker='1764' AND traded_at BETWEEN '2022-09-26' AND '2022-10-03' ORDER BY 1"
```

分類は「asof に価格が付いていたか」を panel の `close` で判定するので、`entry_date` が空でも取りこぼしを `price_gap` として名指しする。bar の読み込み窓を `min(asofs) − 15 日` へ広げて cohort を再構築すると解消する。fin coverage を復元した copy 上での検証（2022-09-30 cohort、3y）:

| 量 | 修正前 | 修正後 |
| --- | ---: | ---: |
| resolved | 3,371 | 3,435 |
| unresolved | 373 | 309 |
| `entry_price_gap` | 64 | 0 |
| `entry_not_listed` | 308 | 308 |
| `unpriced_exit` | 1 | 1 |

resolved が 64 件（1.9%）増える。落ちていたのは薄商いの銘柄であり、割安 screen が候補を見つける側の母集団なので、この取りこぼしは計測に系統的に効く。既存の 46 cohort は forward cache を再構築するまで旧観測を持つため `entry_price_gap` が blocker として残る（再構築は §6 の制約により #670 の後になる）。

## 5. survivorship の実測

計測は `bootstrap-cache` 相当の store 復旧を要するため、**live store を変更せず copy 上で検証**した（§6 の bookkeeping 問題により live store では panel を再構築できない）。

| 量 | 2022-09-30（future master） | 2026-07-28（exact-date master） |
| --- | ---: | ---: |
| as-of 当日に価格が付いた銘柄 | 4,042 | 4,202 |
| master read に不在（population mismatch） | 351（8.7%） | **0** |
| master に在るが政策除外（市場区分外） | 319 | — |
| master に在るが universe へ入れられなかった | 8 | 3 |
| survivorship 判定 | `incomplete` | `complete` |

2022-09-30 の master read は 2026-05-13 の snapshot（`future_snapshot`）なので、2022-09 に上場していて以降に廃止された銘柄が断面から落ちている。これが survivorship bias の実体である。

母集団は **as-of 当日に価格が付いた銘柄**で定義する。as-of 前に最終売買を終えた銘柄を master が持たないのは正しいので、entry の staleness 許容（15 日）をここへ流用すると、正しく除かれた銘柄を「断面が universe を再現していない証拠」として数えてしまう。15 日窓では exact-date master の 2026-07-28 でも mismatch が 3 件残り（3271 / 5903 / 7317、いずれも as-of 前に廃止済み）、`master_snapshot == exact_date` と `survivorship == complete` が同時に成立しない。当日基準ではこの 2 条件が両立する（上表右列）。

`priced_master_without_universe_count` は逆向きの欠けを数える。as-of に価格が付き master にも在るのに panel が評価できなかった銘柄で、exact-date の 2026-07-28 でも 3 件ある。これらは panel に metrics 無しの行として残るため、数えないと「市場に無かった銘柄」と同じ非 block の側へ落ちる。

## 6. panel store が再構築できない

`jquants_fin_summaries` の coverage が 2024-07-17 起点しか主張していないため、`read_fin_summaries` は 2021-08〜2024-07 の行（54,029 行、実在する）を返さない。cohort は `asof − 730 日` の fin 窓を要求するので、**再構築可能な最小 asof は 2026-07-17** となり、grid の最新 cohort（2026-06-30）を含めて **46 cohort すべてが再構築不能**である。

```text
calibration build: 2022-09-30 failed: fin summaries are not covered for 2021-08-02..2022-09-30
calibration build: 2025-08-29 failed: fin summaries are not covered for 2023-08-30..2025-08-29
```

各 panel meta の `effective_fin_start` は当時の coverage 床を記録しており、2022-09-30 cohort では `2021-08-02` である。したがって build 時点では 2021-08-02 起点の coverage が主張されていた。現在の単一 window は、その主張が後の range fetch で置換された状態にあたる。

この状態は「documented な修復経路（`calibration-build --force`）が store の全域で失敗する」という形で現れ、cache schema を変える変更が既存 cohort を復元不能にする。本 PR は cache schema を変えず、判定を評価時導出にすることでこの制約を回避した。

## 7. 残る前提（人間の判断が要るもの）

| 前提 | 解けるもの | 判断事項 |
| --- | --- | --- |
| 取得可能履歴の窓 | `master_snapshot` / `input_range_clamped` / `survivorship` | J-Quants plan。3y は bars 2020-04 起点、5y は 2018-04 起点が必要で、Light（5 年 = 2021-07 起点）では届かない。Standard（10 年）以上を要する |
| 廃止 exit value の source | `unpriced_exit` | JPX 上場廃止一覧・統計月報 archive の採用可否。#420 の feasibility 判定を先に通す |
| fin coverage bookkeeping | `entry_price_gap` / panel の再構築可能性 | 再取得（provider、数時間規模）か、panel meta の attestation に基づく復元か（#670） |

`backfill-master --month-end-from/--month-end-to` は、窓の前提が満たされた時点で断面 master を 1 cohort = 1 request で埋める経路として用意した（`bootstrap-cache` は 1 日あたり数時間）。

## 8. 変更していないもの

- screening rules / E[r] / selection / ranking / proposal
- horizon authority の要求（3y と 5y の双方 eligible を production 変更の関門とする契約）
- `data/screening/market.sqlite`（read-only 参照のみ。§5 の検証は copy 上で実施）
- calibration cache の schema version（`2` を維持）

## 9. 監視事項

- 断面 master の保存は 2026-07-15 以降の営業日しかないため、grid 上の月末（2026-05-29 / 2026-06-30）は `backfill-master` を実行しない限り exact-date にならない。日次バッチが月末を跨いだ後の cohort（2026-07-31 以降）では自然に exact-date になるので、次回計測で `master_snapshot` blocker が消えることを確認する。
- `unpriced_exit` の件数は cohort の窓が伸びるほど増える（2023-06-30 cohort で 18 件）。廃止 exit value source の必要性はこの数列で追う。
