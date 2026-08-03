---
title: "Estimate calibration"
summary: "point-in-time panelと長期forward returnでE[r]・FV・selection方法を較正するcontract。"
doc_type: reference
status: active
last_reviewed: 2026-08-03
---

# estimate-calibration

screening の機械見積りと選定順位を過去 as-of で再構成し、価格リターンの実現値へ突き合わせる local-only の較正処理である。portfolio outcome や JPX total-return benchmark とは別の、cross-sectional な estimator diagnostic を所有する。

## Horizon authority

| horizon | target | authority |
| --- | --- | --- |
| `3m` / `6m` | calendar month addition | regression alert |
| `1y` | calendar year addition | leading evidence |
| `3y` / `5y` | calendar year addition | production decision evidence |

`calibration-evaluate` の通常実行は全 horizon を diagnostic として出力する。実証的な screen、ranking、E[r] policy parameter の変更候補は、`--run-purpose production_decision` で明示した required as-of と required metric に対して、3y と 5y の双方が eligible のときだけ検討できる。artifact は設定やコードを自動変更しない。

target は cohort の actual as-of date に calendar month を加算する。元の日が calendar month-end の場合は対象月末を保ち、非取引日は target 以下の最終取引日に解決する。

## Data integrity

panel は cohort as-of 以下の最新 `eq_master` snapshot だけを読む。prior snapshot、snapshot unavailable、survivorship、delisting、corporate-action event coverage の不備は payload に残り、3y/5y evidence を block する。

forward row は `resolved` または明示的な unresolved status を持つ。target と entry はそれぞれ target/as-of 以下の最終取引日で解決し、15 日超の stale exit は resolved return に入れない。価格は as-of basis adjustment factor で正規化するが、metric basis は `price_return_only` であり配当 accrual を加えない。entry 時点の配当利回りを horizon 年数で按分する固定 accrual は、期間中の増配・減配・無配・支払時期を観測した実現配当ではないため、実現値として扱わない。

<a id="coverage-verdicts"></a>

### Coverage 判定の導出

3 つの coverage は cache された観測から評価時に導出する。cohort が書かれた時点の契約ではなく現行契約で判定するためであり、既存 cohort も再構築せずに判定し直せる。

3y/5y の blocker は 1 観測につき 1 つだけ立てる。ある判定から導ける別の判定を並べると、同じ欠けを二重に数えて理由の内訳が読めなくなるためである。

| 観測 | 測るもの | blocker が立たない条件 |
| --- | --- | --- |
| `master_snapshot_status` | population が cohort 日の断面から来ているか | `exact_date` |
| `survivorship_coverage_status` | panel の population が as-of の投資可能 universe を再現しているか | `asof_population_mismatch_count == 0` |
| `priced_master_without_universe.direction_stable` | as-of に価格が付き master にも在るが panel が評価できなかった銘柄が結論を作っていないか | diagnostics 件数と row 同定数が一致し、`true` |
| `adjustment_factor_coverage` | 価格系列に分割調整 factor が揃っているか | `complete` |
| `delisting_exclusion.direction_stable` | 窓中に価格が途切れた銘柄の除外が結論を作っていないか | `true` |
| `entry_price_gap_count` / `future_horizon_count` / `unclassified_unresolved_count` | 未解決 row の分類（下記） | `0` |
| `input_range_clamped` / `candidate_partition_complete` | 入力窓が要求長を満たし、panel と forward の銘柄集合が一致するか | clamp なし / 一致 |

survivorship は population の性質なので panel が件数を測り、verdict は読み手が件数から導く（凍結すると complete の定義を変えたときに既存 cohort へ届かない）。bar store は市場から消えた銘柄の価格も保持するため、**as-of 当日に価格が付いた集合**を master snapshot と独立に観測できる。master が as-of より後なら当時上場していて現在は廃止された銘柄を欠き、前なら以降に上場した銘柄を欠く。どちらも断面が as-of の投資可能 universe ではないので incomplete とする。当日を基準にするのは、as-of 前に最終売買を終えた銘柄を master が持たないのは正しいからで、entry の staleness 許容（15 日）をここへ流用するとどの master でも mismatch を 0 にできなくなる。計測前に書かれた panel は件数を null として報告する（未計測を「欠けなし」と読めないようにする）。

`adjustment_factor_coverage` は bar store が答えられる唯一の corporate-action 観測である。系列を調整しない action（合併の対価、株主割当増資）はローカルに source が無いので、この残余は判定に畳まず、外部 source を要する既知の限界として扱う。

`unpriced_exit` / `adjustment_factor` の verdict は、survivorship が complete な断面でのみ意味を持つ。population から既に落ちている銘柄については系列終了も factor 欠落も観測され得ないので、survivorship が incomplete な cohort でこの 2 つが `complete` に見えるのは「濾された後の集合が綺麗」という意味にすぎない。

### 未解決 row の分類

未解決 row は 1 種類の欠陥ではないので、authority gate は総数ではなく分類ごとの件数を読む。

| 分類 | 意味 | gate への影響 |
| --- | --- | --- |
| `entry_not_listed_count` | panel も as-of の価格を持たない | block しない。production screen も同じ銘柄を universe から落とすので、較正の母集団は screen が選び得た集合と一致する |
| `entry_price_gap_count` | panel は as-of の価格を持つのに forward が entry を持たない | block する。断面に数えた銘柄の forward 観測が無いので、population を無言で欠く |
| `unpriced_exit_count` | 窓中に系列が終わる（廃止 exit value なし） | 件数では block しない。`delisting_exclusion` が結論の頑健性で判定する（下記） |
| `future_horizon_count` | target が評価可能な最終取引日より先 | block する。cohort が満期に達していない |
| `unclassified_unresolved_count` | 上のどれにも入らない未解決 status | block する。分類は allowlist なので、status が増えた日に無音で通らないための残余 |

`entry_not_listed` が非 block なのは「市場に無かった」に限らないので、panel が price を持ちながら universe へ入れられなかった銘柄は row の `population_coverage_status` と `priced_master_without_universe_count` で別に同定する。universe の除外条件が増えても、その分が非 block の側へ黙って流れ込まない。

entry は as-of の 15 日前までの close で解決するので、保有期間は名目 horizon より最大でその分長い。この許容が効く範囲まで bar の読み込み窓を広げてあり、`adjustment_factor_coverage` を判定する bar 集合も同じ窓に従う。

### universe 未評価銘柄（`priced_master_without_universe`）

該当 row は必要な入力履歴を欠くため valuation metrics、rank、E[r] を持たず、窓中に価格系列が終われば実現 forward return も持たない。現行 method で選抜対象にならない row へ所属を後付けせず、観測済み return の有無が母集団中央値を通じて production 結論の向きを作っていないかを有界バイアスで判定する。

報告値では観測済み return だけを使い、未解決 return は値なしのまま母集団から除外する。感度計算では resolved / unresolved を問わず該当 row だけを `-1.0` と置換前の resolved 流動性母集団中央値へそれぞれ置換する。`recommended_rank_top5` / `top10` と `er_calibration` の向きは delisting 判定と同じ定義を使い、報告値と両置換の向きがすべて一致するときだけ `direction_stable` とする。`resolved_target_count` と `resolution_complete` は観測できた実現 return の coverage 診断であり、単独では authority を block しない。

cache が対象 row を同定できない、diagnostics 件数と row 数が一致しない、または両側代入で向きが割れる場合は fail closed で block する。この判定は欠けた実現 return、metrics、rank を復元せず、未評価銘柄が無かったことにもならない。

### 廃止銘柄の除外（`delisting_exclusion`）

authoritative な delisting exit value source が無い限り、窓中に系列が終わる銘柄は exit value を持たないまま cohort から落ちる。満期済み cohort は例外なくこれを含むので、件数で block すると 3y/5y の evidence は原理的に成立しない。代わりに、その除外が結論を作ったかどうかを cohort ごとに判定する。

除外された銘柄へ範囲の両端を代入して結論を再計算し、**cohort が報告した値と両方の代入とで向きが一致するときだけ** `direction_stable` を立てる。

| 代入 | 値 |
| --- | --- |
| 全損 | `price_return = -1.0` |
| 中立 | 同 cohort の resolved 銘柄の中央値 |

報告値を比較に含めるのは、それが authority gate の読む値そのものだからである。両方の代入で向きが揃っても報告値だけが逆を向くなら、その結論は除外が作ったものになる。向きは `recommended_rank_top5` / `recommended_rank_top10` が group の `median_excess` の符号、`er_calibration` が最上位 quintile の `median_realized_price_excess` − 最下位 quintile の同値の符号で定める。いずれかの場合で値が算出できず他の場合で算出できるときも、除外が「cohort が何か言えるかどうか」を決めているので不安定として扱う。

この判定は結論を下へ引く可能性に対しての bracket である。買収による廃止はプレミアム付きで中立代入の上に出るため、上側は挟まない。exit value そのものを外部 source から取る道は別に残る。

`er_calibration` は価格収束成分 `er_reversion_annual` だけを price-only 実現値へ較正する。予測値は cohort 内の `er_reversion_annual` 中央値、実現値は同じ cohort の price return 中央値をそれぞれ引き、quintile ごとに median の相対値を比較する。`calibration_error` は `realized - predicted` である。配当と buyback の carry は price-only 実現値と同じ basis で観測できないため、この座標で絶対水準を較正しない。carry の妥当性は source と算出 contract を検証し、実現配当を備えた total-return dataset が利用できる場合に別の較正座標で扱う。

`er_level_calibration` は E[r] 合計の絶対年率と、実績 FY 配当を加えた実現 total return の絶対年率を `er_annual` quintile ごとに比較する。実現配当は `entry_date < fiscal_year_end <= exit_date` の FY 行を対象に、同じ FY の最新 non-null `DivAnn` を forward store の最終 bar 株式基準へ正規化して合算する。対象 FY 行なし、`DivAnn` 欠損、adjustment factor 不完全は 0 円とせず total-return 側を unresolved にする。明示された `DivAnn == 0` は観測済み無配である。端の FY は月割りしないため、この座標は実際の中間・期末配当の権利落ち日を再現する cash-flow ledger ではない。

Shortlist の判断面が読む最新文脈の正本は `reports/data/er-level-calibration-latest.yaml` である。`calibration-evaluate --context-out` が、production authority の成立した明示的な required scope だけから 3y / 5y の quintile 表、各帯の上端、cohort as-of 範囲、`screening_rules_hash`、`er_model_version` を生成する。UI は artifact の2つの method identity が現在の production method に加えて、実際に表示する operative run の不変 method identity と一致するときだけ、3y の帯へその run の機械 E[r] を対応づけ、同じ帯の歴史実現中央値を参考表示する。E[r]、順位、gate、FV は変更しない。値は個別銘柄の予測ではなく、重複する月次窓と COVID 前後に偏る historical panel の cohort 中央値である。

artifact は生成日から45日だけ有効とし、月次の calibration 更新後に同じ production scope の評価から再生成する。欠損、schema / basis / quintile 境界不正、現在 method または operative run との identity 不一致、run identity 不明、未来日、45日を超える期限、期限切れでは read model が文脈全体を非表示にする。YAML を手編集して更新しない。

forward row は price-only の `price_return` / `status` と、`realized_dividend_sum` / `realized_dividend_fy_count` / `total_return` / `total_return_status` / `total_return_basis` を別々に持つ。`total_return_status == resolved` の row だけが level metric に入り、既存 price-only metric の母集団と値は変えない。component 表の realized dividend は annualized(total) − annualized(price) で、予測 carry に含まれる buyback を直接観測しない。

`er_level_calibration`、`margin_deadline_gate_top10`、`margin_short_to_adv`、`normalized_per_3fy` は production core metricではなくoptionalな既知metricである。各metricをproduction判断に使う事前登録済みrunは、core 3 metricと併せて対象を`--required-metric`へ明示する。

cache schema version は `10`。panel は8つの point-in-time quality condition、6成分以上を観測できる行だけの `quality_signal_count`、E[r] top-decile 内の high/low interaction を持つ。さらに、production の730日財務入力を変えずに補助履歴から、3 FY の split-safe DPS、DPS YoY・予想増配・配当開始、グロス株数減少 streak と還元変化 composite、および赤字を含む連続3/5 FYのsplit-safe平均EPSによる正規化PERと3 FY cycle positionを記録する。グロス株数減少は自己株取得の事実ではなく、消却・発行等の純変化 proxy である。

信用需給では、貸借銘柄だけの `margin_short_to_adv`、時価総額 quintile 内の `margin_long_to_adv` percentile、交絡確認用の60取引日 realized volatilityを保持する。`margin_std_long_share >= 0.75` の recommendation-only virtual gateは、candidates・full rankを変えずに除外後を詰めた top-5 / top-10 をbaselineと比較する。production判断では、virtual gateは`margin_deadline_gate_top10`、空売り残/ADVのraw annotationは`margin_short_to_adv`をcore 3 metricと併せて明示する。missing/mismatch/partial cache は `calibration-build --force` で再構築する。旧 reader は提供しない。

### pre-2019 診断 panel

`--panel-variant pre2019_self_range_375` は self-range を 375 sessions、bar 入力を 600 暦日に固定する診断専用 contract である。通常 store と異なる `--calibration-dir` が必須で、variant と窓は `rules_hash` に含まれ、全 row が `self_range_degraded: true` を持つ。この store を `--run-purpose production_decision` で評価すると拒否する。production panel の既定窓、screening rules、authority 条件は変わらない。

同じ分離契約で `self_range_1250`（1,250 sessions / 2,000暦日）と`self_range_2500`（2,500 sessions / 4,000暦日）を診断できる。各rowの`self_range_observed_sessions`は上限へ実際に届いたかを示し、短い履歴をfull-windowとして扱わない。いずれもdiagnostic-onlyで、production self-rangeは750 sessionsのままである。

```bash
uv run baibai-engine screening calibration-build \
  --start 2018-03-01 --end 2019-10-31 \
  --calibration-dir data/screening/calibration-pre2019 \
  --panel-variant pre2019_self_range_375 --force
uv run baibai-engine screening calibration-evaluate \
  --calibration-dir data/screening/calibration-pre2019 \
  --horizon 1y --horizon 3y --out /tmp/calibration-pre2019.yaml
```

## Commands

```bash
uv run baibai-engine screening backfill-master --month-end-from 2022-09-01 --month-end-to 2026-06-30
uv run baibai-engine screening calibration-build --start 2023-01-01 --end 2026-04-30 --force
uv run baibai-engine screening calibration-evaluate --out .cache/calibration-eval.yaml
uv run baibai-engine screening calibration-evaluate \
  --run-purpose production_decision \
  --required-asof 2021-06-30 \
  --required-metric recommended_rank_top5 \
  --required-metric recommended_rank_top10 \
  --required-metric er_calibration
```

E[r] 水準 parameter を判断する事前登録済み run では、上の core 3 metric に加えて `--required-metric er_level_calibration` を指定する。判断面の月次文脈も更新する run は、同じ command に `--context-out reports/data/er-level-calibration-latest.yaml` を加える。authority が不成立、required cohort が不足、level metric が未解決の場合は context を書かず exit 1 にする。

The retained diagnostics are selection top-5/top-10 median excess and trap rate, price-reversion E[r] relative calibration, FY-dividend total-return E[r] level calibration, axis/gate/reversion regression diagnostics, and cohort coverage/integrity. They do not establish a track record or statistical significance.

## 改善サイクルの運用契約

基盤（マクロ読み・screening 選定・E[r]/FV/RR 見積り）の改善は独立した運用 loop を持たず、**self-contained issue → 通常の PR delivery** で回す。1 改善 = 1 issue = 1 PR。issue には観察（レポート参照つき）→ 仮説 → 検証方法 → 着手条件と、冒頭に `価値tier: Tn — <因果経路>`（[doctrine](../doctrine.md#improvement-value-hierarchy)）を書く。

### 改善対象マップ（レバーの所在）

| レバー | 所在 | 計測経路 |
| --- | --- | --- |
| screen の閾値・gate・evidence pattern | `method/screening-rules/*.yaml` | 較正リプレイ（rules variant） |
| select の順位付け・diversity cap | 同上 + `src/baibai_engine/screening/selection/` | 較正リプレイ（selection replay） |
| 機械 E[r]・FV アンカー | `src/baibai_engine/screening/estimates.py` | 較正リプレイ（er 軸 IC / decile / 予測 vs 実現） |
| valuation 指標の算出 | metrics 系 + [`valuation-metrics.md`](./valuation-metrics.md) | 較正リプレイ（軸別 IC / coverage） |
| マクロ読みの手順・レンズ | [`macro.md`](./macro.md) + skill `macro-context` | 保有 outcome / 月次の事後検証（N≈1、統計計測はしない） |
| research の見積り手順 | [`thesis.md`](./thesis.md) + skill `research` | portfolio outcome と長期 horizon calibration |
| 資本・cap・sizing | [`portfolio-management.md`](../portfolio-management.md) + `position/policy.py` | 保有 outcome |
| OP3 の選定判断 | skill `shortlist` の深度契約 | 判断コホート比較（`screening shortlist outcome`）+ 機会費用計測 tools |

evidence pattern（playbook）を追加・変更・削除するときは、screening rules・対応 checklist・selection の順位・test を同じ変更で整合させ、根拠を較正結果に置く。

### 事前登録と design/confirm

- 採用 judge になる数値基準は**計測を実行する前に** issue または report 冒頭へ書いて commit する（git history が事前登録の正本）。既知の結果がある場合は盲検性の限定を正直に書く。
- cohort を時間で design / confirm に 2 分割し、**両方で同方向・基準充足のときだけ採用**。片側のみは不確定、両側逆は棄却。grid search（基準を後から動かす網羅探索）をしない。
- **control cell の判定は「0 許容の全 cell 通過」を既定にしない**（偽陰性へ構造的に偏る）。noise floor（例: trap delta ≤ +2pt）または k-of-n cell 通過と、cell ごとの最小 matched weight を**事前登録で宣言**する。
- 判定語彙は `negative` / `insufficient` / `adoption_candidate` / `inconclusive` の 4 種。同一仮説の再検定は新 evidence（新規満期 cohort・contract レベルの capacity 変更）がある場合に限る。
- `negative` / `inconclusive` が確定した軸は、判定 PR で panel 列・派生計算・評価枝・専用 test を削除し、dated report と git history を反証証跡の正本とする（残すのは `adoption_candidate` / `insufficient` / control 再利用列 / production annotation 入力列のみ）。
- rules variant の計測は本番 rules を変えず `SCREENING_RULES_PATH` で variant を指し、別 store（`data/screening/calibration-<variant>/`）へ panel を構築する。rules_hash provenance が混線を機械検出する。
- 機械レバー（screen / select / E[r]）の実証的改訂は 3y/5y eligible evidence を必須の関門にし、判断レバー（macro / research 手順）は保有 outcome と運用の事後検証で改める。

### 採用後

- 通過した変更だけを本番へ反映し、計測した構成と本番構成を一致させる。rules 改訂後は panel を `--force` 再構築する。
- 現 asof で `screening run` → `select --longlist-top 20` を回し、意図した挙動を実銘柄で確認する（運用テスト）。
- `reports/YYYY-MM-DD-<slug>.md` に再現手順・データ窓・coverage / survivorship 開示・判定表・検算・採用後の監視事項を固定する（一次計測記録。別の監査ファイルは作らない）。マージ前 gate は [`python-foundation.md`](./python-foundation.md) §9 が正本。マージ後は report の監視事項を次の replay 計測で追う。

### 判断コホートの集計

primary-research lane の research FV と screening FV の bridge は、有効観測（同一 thesis 再実行・scaffold-only・未 review・遡及記入を除く）が 5 件以上になったら乖離率の中央値・範囲・要因件数・coverage を記述集計する。この集計だけで screening 式を変えず、変更仮説は別 issue で事前登録して design/confirm へ進める。四半期ごとに shortlist rejected と assessment reject / defer の `reject_class` 頻度を集計し、機械化可能な型を warning / flag 候補として事前登録する（分類で自動除外・ranking 変更はしない）。

### 誠実性の規律

1. 有意性・統計的優位を主張しない。効果量と cohort 勝率で判断し、そう書く。
2. 仮説と採否基準は検証前に事前登録し、後から動かさない。
3. survivorship・coverage の欠け・レジーム文脈を計数で開示する。
4. 累積リターン・年率・シャープ等を track record として掲げない。
5. post-hoc の判断はそう明記する。
