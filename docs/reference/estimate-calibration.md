---
title: "見積り較正"
summary: "point-in-time panelと長期forward returnでE[r]・FV・selection方法を較正する契約。"
doc_type: reference
status: active
---

<a id="estimate-calibration"></a>

# estimate-calibration — 見積り較正

本書は、screeningの機械見積りと選定順位を過去as-ofで再構成し、実現した価格リターンと突き合わせるローカル専用の較正契約を所有する。portfolio outcomeやJPX total-return benchmarkとは別の横断的な見積り診断である。

<a id="horizon-authority"></a>

## Horizonごとの判断権限

| horizon | targetの決め方 | 判断権限 |
| --- | --- | --- |
| `3m` / `6m` | 暦月を加算 | regression alert |
| `1y` | 暦年を加算 | leading evidence |
| `3y` / `5y` | 暦年を加算 | production decision evidence |

`calibration-evaluate` の通常実行は全 horizon を diagnostic として出力する。実証的な screen、ranking、E[r] policy parameter の変更候補は、`--run-purpose production_decision` で明示した required as-of と required metric に対して、3y と 5y の双方が eligible のときだけ検討できる。artifact は設定やコードを自動変更しない。

target は cohort の actual as-of date に calendar month を加算する。元の日が calendar month-end の場合は対象月末を保ち、非取引日は target 以下の最終取引日に解決する。

<a id="data-integrity"></a>

## データ完全性

panel は cohort as-of 以下の最新 `eq_master` snapshot だけを読む。prior snapshot、snapshot unavailable、survivorship、delisting、corporate-action event coverage の不備は payload に残り、3y/5y evidence を block する。

cohort manifest は build が実際に読んだ sealed SQLite snapshot の identity を記録する。snapshot bytes
自体は保持しない。production decision の可否は、保持している panel / forward / diagnostics の
integrity、3y/5y coverage、required metric と measurement policy で決める。将来の code で過去入力を
完全再実行できるという別の保証は要求しない。

forward row は解決済み status（市場終値による `resolved`、成立した現金公開買付けによる `resolved_control_event_exit`、破綻型の上場廃止による `resolved_failure_exit`）または明示的な unresolved status を持ち、`resolved` flag は前者 3 つと一致する。target と entry はそれぞれ target/as-of 以下の最終取引日で解決し、15 日超の stale exit は resolved return に入れない。価格は as-of basis adjustment factor で正規化するが、metric basis は `price_return_only` であり配当 accrual を加えない。entry 時点の配当利回りを horizon 年数で按分する固定 accrual は、期間中の増配・減配・無配・支払時期を観測した実現配当ではないため、実現値として扱わない。

財務サマリーの購読窓は 10 年の移動窓であり、store が読み取りを許す最古の日付は日々進む。panel の履歴窓（正規化 EPS 2,200 日、株主還元 1,200 日）はこの下限で切られるので、下限に近い古い cohort ほど履歴が短く、必要な期数に届かない値は null で出る。窓が通り過ぎた行は table に残るが読まない。**下限は store が持つ最古の行ではなく coverage が答える範囲から取る。** 両者は同じ「履歴の始まり」を指しながら別の量であり、行の側を採ると source が出せない範囲を要求して全 cohort が構築不能になる。

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
| `edinet_axis_population_count` | EDINET の書類から作る軸（`ev_ebitda` / `net_cash_to_market_cap` / `fcf_yield` / `asset_backed_ratio`）を持つ母集団の行数 | 件数として読む（blocker は立てない） |

survivorship は population の性質なので panel が件数を測り、verdict は読み手が件数から導く（凍結すると complete の定義を変えたときに既存 cohort へ届かない）。bar store は市場から消えた銘柄の価格も保持するため、**as-of 当日に価格が付いた集合**を master snapshot と独立に観測できる。master が as-of より後なら当時上場していて現在は廃止された銘柄を欠き、前なら以降に上場した銘柄を欠く。どちらも断面が as-of の投資可能 universe ではないので incomplete とする。当日を基準にするのは、as-of 前に最終売買を終えた銘柄を master が持たないのは正しいからで、entry の staleness 許容（15 日）をここへ流用するとどの master でも mismatch を 0 にできなくなる。計測前に書かれた panel は件数を null として報告する（未計測を「欠けなし」と読めないようにする）。

EDINET の取込は最近の as-of 分しか無いので、それ以前の cohort は `edinet_axis_population_count` が 0 になる。0 の cohort は EDINET 由来の軸を 1 つも持たずに screen を再現しており、production は 4 軸の**いずれか**を母集団の 87.1% で持つ（as-of 2026-07-31 で 1,339/1,537。軸別は `ev_ebitda` 69.2% / `net_cash_to_market_cap` 77.9% / `fcf_yield` 69.2% / `asset_backed_ratio` 69.3%、4 軸すべては 51.8%）。件数は「いずれか」なので、3 軸しか無い cohort と 4 軸ある cohort を区別しない。**その cohort が測っているのは、production が実際に走らせている screen とは入力の違う screen である。** 件数を出し、判定は読み手が導く（blocker にすると長期 horizon の evidence が原理的に成立しない）。`null` は計測前に書かれた panel で、0（観測して 1 件も無い）と読み替えない。

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

entry は as-of の 15 日前までの close で解決するので、保有期間は名目 horizon より最大でその分長い。価格 bar と `adjustment_factor_coverage` はこの窓から読む。一方、実現配当を支払ごとに株式基準へ換算する corporate-action event は、最初に対象になる FY の `period_start` まで別に遡って読む。価格入力窓と配当の会計期間窓は同一ではない。

### universe 未評価銘柄（`priced_master_without_universe`）

該当 row は必要な入力履歴を欠くため valuation metrics、rank、E[r] を持たず、窓中に価格系列が終われば実現 forward return も持たない。現行 method で選抜対象にならない row へ所属を後付けせず、観測済み return の有無が母集団中央値を通じて production 結論の向きを作っていないかを有界バイアスで判定する。

報告値では観測済み return だけを使い、未解決 return は値なしのまま母集団から除外する。感度計算では resolved / unresolved を問わず該当 row だけを `-1.0` と置換前の resolved 流動性母集団中央値へそれぞれ置換する。`recommended_rank_top5` / `top10` と `er_calibration` の向きは delisting 判定と同じ定義を使い、報告値と両置換の向きがすべて一致するときだけ `direction_stable` とする。`resolved_target_count` と `resolution_complete` は観測できた実現 return の coverage 診断であり、単独では authority を block しない。

cache が対象 row を同定できない、diagnostics 件数と row 数が一致しない、または両側代入で向きが割れる場合は fail closed で block する。この判定は欠けた実現 return、metrics、rank を復元せず、未評価銘柄が無かったことにもならない。

### 廃止銘柄の除外（`delisting_exclusion`）

窓中に系列が終わる銘柄のうち、成立した現金公開買付けが対価を確定させたものは実値で解決する（後述「支配権イベントの実現 exit 値」）。それ以外は exit value を持たないまま cohort から落ちる。株式の併合・株式等売渡請求だけで完結した廃止、倒産、資料履歴外の廃止がそこに残り、満期済み cohort は例外なくこれを含むので、件数で block すると 3y/5y の evidence は原理的に成立しない。代わりに、その除外が結論を作ったかどうかを cohort ごとに判定する。

除外された銘柄へ範囲の両端を代入して結論を再計算し、**cohort が報告した値と両方の代入とで向きが一致するときだけ** `direction_stable` を立てる。

| 代入 | 値 |
| --- | --- |
| 全損 | `price_return = -1.0` |
| 中立 | 同 cohort の resolved 銘柄の中央値 |

報告値を比較に含めるのは、それが authority gate の読む値そのものだからである。両方の代入で向きが揃っても報告値だけが逆を向くなら、その結論は除外が作ったものになる。向きは `recommended_rank_top5` / `recommended_rank_top10` が group の `median_excess` の符号、`er_calibration` が最上位 quintile の `median_realized_price_excess` − 最下位 quintile の同値の符号で定める。いずれかの場合で値が算出できず他の場合で算出できるときも、除外が「cohort が何か言えるかどうか」を決めているので不安定として扱う。

この判定は結論を下へ引く可能性に対しての bracket である。買収による廃止はプレミアム付きで中立代入の上に出るため、上側は挟まない。実値で解決できた行はこの bracket の対象から外れる。

## 較正座標

### 価格収束E[r]

`er_calibration` は価格収束成分 `er_reversion_annual` だけを price-only 実現値へ較正する。予測値は cohort 内の `er_reversion_annual` 中央値、実現値は同じ cohort の price return 中央値をそれぞれ引き、quintile ごとに median の相対値を比較する。`calibration_error` は `realized - predicted` である。配当と buyback の carry は price-only 実現値と同じ basis で観測できないため、この座標で絶対水準を較正しない。carry の妥当性は source と算出 contract を検証し、実現配当を備えた total-return dataset が利用できる場合に別の較正座標で扱う。

### Cohort比較のbasis

cohort 比較（`tools.experiments.measure_signal_cohorts`）は `--basis price|total` の両方を取る。carry は配当と自己株買いでできているので、その効果量を price basis で測ると払われた現金の分だけ小さく出る。ただし total は窓内の FY 配当観測を要し、母数は horizon で変わる（price 側に対し 1y で 95%、3y で 93%、5y で 88%、3m / 6m は半分未満）。出力の `basis_coverage` が horizon ごとの両母数と `bases_comparable` を出し、被覆が足りない horizon で 2 つの中央値を並べて読むことを禁じる。既定は price のままで、これは全 horizon で解決するのが price 側だけであるため。

### 自己株式取得枠

自己株取得を含む資本配分は、選ばれた銘柄のresearchで一次開示を読む。production rankingが使う`net_share_change_yoy`は過去の株数変化・希薄化signalであり、将来のbuyback cashや未消化枠とは呼ばない。

### Evidence Patternの閾値とgate

閾値座標`evidence_pattern_thresholds`は、Evidence Patternが採用した銘柄と、同じEvidence Patternの他条件をすべて満たしながらその閾値1本だけで落ちた銘柄の実現超過を並べる。落ちた側は`rules.threshold_blocks`が決める。判定は閾値を無効化したconfigで同じEvidence Pattern判定関数を呼び直して得るので、条件の意味もnullの扱いも`rules.py`の1か所にとどまり、座標側に書き写さない。2本以上の閾値で落ちた行はどちらの閾値も選んでいないので、どちらの群にも入らない。欠損や除外業種で判定できない行も同様に入らないため、この座標は閾値の水準を測り、null方針は測らない。cohort横断では平均効果量と、採用側が上回ったcohortの比率を出す。

`gates` 座標は deterioration gate を割安 decile 内で通過群と非通過群に分けて測り、cohort 横断で同じ形の集計を持つ。

### 業種中央値basis

`sector_median_basis` 座標は `smg_*` 軸を、業種中央値から作られた行と市場中央値へ落ちた行に分けて測る。素性は `PanelRow.smg_market_fallback` が持つ。落ちる業種は構造的に低倍率へ寄る側に集中するため、分けないと業種の割安と業種構成が同じ数字に混ざる。

**群の水準と軸の効きは別の量として出す。** 落ちるかどうかは業種単位で決まるので市場側の群は業種の集合そのものであり、その中央値超過（`group_median_excess`）はその業種構成である。実データで各行の自業種中央値を引くと、市場側の水準は全軸・全 cohort で 0 になる。軸の効きは群の中を軸値で 2 分割した差（`axis_effect.median_excess_delta`）で測る。両側が同じ業種集合から引かれるので、構成が作れる差はごく小さい（業種内 shuffle null で +1.1〜+2.9pp、観測は +7.0〜+11.7pp、p=0.000）。市場側は 1 cohort あたり 50〜80 行で decile を組めないため `decile_spread_median` は出ないが、2 分割は分位あたり 15 行以上を保てるので両側で成立する。**`mean_axis_effect` は `stdev_axis_effect` と併せて読む。** 月末 as-of の 1y 窓は大きく重なるため独立な窓は年数程度しかなく、cohort 数だけ独立観測があるようには読めない。2 つの basis は名前の集合そのものが違うので、basis 間で `mean_axis_effect` を直接比べると軸の効きと 9 業種の振る舞いが混ざる。

2 つの側は母数が違う。母数下限を割る業種は 9 つしかないので、cohort あたり自業種が数千行に対し市場側は 50 行前後になる。50 行の decile は 1 分位 5 件なので decile spread は標本が足りる cohort でのみ併記し、cohort 横断集計は spread を出した cohort 数 (`spread_cohorts`) を cohort 数と別に持つ。spread が出せなかったことと効果が無かったことを混同させないためである。群統計 (n / median / mean / trap rate) はその群が何だったかを記述するが、比較はしない。比較は上記の `axis_effect` が担う。

### E[r]絶対水準

`er_level_calibration` は E[r] 合計の絶対年率と、実績 FY 配当を加えた実現 total return の絶対年率を `er_annual` quintile ごとに比較する。実現配当は `entry_date < fiscal_year_end <= exit_date` の FY 行を対象に、同じ FY の最新 non-null `DivAnn` を forward store の最終 bar 株式基準へ正規化して合算する。対象 FY 行なし、`DivAnn` 欠損、adjustment factor 不完全は 0 円とせず total-return 側を unresolved にする。明示された `DivAnn == 0` は観測済み無配である。端の FY は月割りしないため、この座標は実際の中間・期末配当の権利落ち日を再現する cash-flow ledger ではない。

## 判断面へ渡す較正文脈

Shortlist の判断面が読む最新文脈の正本は `reports/published/er-level-calibration-latest.yaml` である。`calibration-evaluate --context-out` が、production authority の成立した明示的な required scope だけから、3y / 5y の固定 E[r] quintileを生成する。各帯は実績 FY 配当込み total return を主 basis、price-only を副 basis とし、ticker-as-of 等重みの絶対年率 median / q25 / q10 / trap rate / n、cohort 等重みの同じ統計、median n、cohort 数を持つ。

UI と research workspace は、artifact の `screening_rules_hash` と `er_model_version` が実際に表示・調査する operative run / selection の identity と一致するときだけ、候補 E[r] を該当 quintileへ対応づける。E[r]、順位、gate、FV は変更しない。値は個別銘柄の予測ではなく historical distribution であり、重複する月次窓を独立標本と呼ばない。

artifact は生成日から45日だけ有効とし、月次の calibration 更新後に同じ production scope の評価から再生成する。欠損、schema / basis / quintile 境界不正、現在 method または operative run との identity 不一致、run identity 不明、未来日、45日を超える期限、期限切れでは read model が文脈全体を非表示にする。YAML を手編集して更新しない。

`calibration-evaluate` の artifact は各 `(asof, horizon)` の `integrity_status`、required metric別 status、blocking reasonを `cohort_integrity` に持つ。study はこの評価結果を読み、独自の辞書リテラルで eligibility を作らない。

forward row は price-only の `price_return` / `status` と、`realized_dividend_sum` / `realized_dividend_fy_count` / `total_return` / `total_return_status` / `total_return_basis` を別々に持つ。`total_return_status == resolved` の row だけが level metric に入り、既存 price-only metric の母集団と値は変えない。component 表の realized dividend は annualized(total) − annualized(price) で、予測 carry に含まれる buyback を直接観測しない。

## Methodとcacheの互換性

`er_level_calibration`、`margin_short_to_adv`、`normalized_per_3fy` は production core metricではなくoptionalな既知metricである。各metricをproduction判断に使う事前登録済みrunは、core 3 metricと併せて対象を`--required-metric`へ明示する。

cache schema versionは互換性を決める入力から導出する（panel / diagnostics / forwardのfield、測るEvidence Pattern閾値、gate軸、sector-gap軸）。市場storeの`user_version`と同じく自動で進むので、列の形を変えずに観測の範囲だけ広げた変更でも版が動く。手で宣言する識別子は`VALUATION_CALCULATION_REVISION`だけで、式の意味の変更は内容から導けないためそこだけ人が進める。panelは、productionの730日財務入力を変えずに補助履歴から、3 FYのsplit-safe DPS、DPS YoY・予想増配・配当開始、グロス株数減少streakと還元変化composite、赤字を含む連続3/5 FYのsplit-safe平均EPSによる正規化PER、PIT-TTMの`operating_profit_to_assets`・`operating_margin`・`asset_turnover`を記録する。収益性levelはcalibration専用で、productionのcandidate、E[r]、FV、rank、gateへ渡さない。グロス株数減少は自己株取得の事実ではなく、消却・発行等の純変化proxyである。`rules_hash`はrules・variant・入力窓に加えてvaluation calculation revisionを含む。valuationの式・資本分母・価格基準が異なるpanelは、method identityとcache schemaの不一致でfail closedにする。

報告空売り残高の L1 は disclosure date と calculation date を分け、reporter 名tuple、ratio / shares / units、取消、provider row ordinalを保存する。panel の `reported_short_ratio` / `reported_short_breadth` / `reported_short_latest_disclosed_at` は両日が cohort as-of 以下の最新stateだけを集約する。公式 dataset floor から連続coverageを証明できる場合だけ無報告を明示的0とし、plan floor、coverage gap、同率最新stateの競合では該当値をnullにする。0は「0.5%未満または報告不在」であって空売り不存在を意味しない。この軸も calibration annotation 専用である。

信用需給では、公表済みの直近残高（2026-09-18 まで全銘柄週次、以後は全銘柄日次）を source として、貸借銘柄だけの `margin_short_to_adv` と、交絡確認用の60取引日 realized volatilityを保持する。列の語義は cadence で変わらない（[`margin-publication-transition.md`](./margin-publication-transition.md) §6）。`margin_std_long_share` は判断面へ出す文脈 annotation である。`selection.supply_demand.margin_std_long_share_exclude_at_or_above` は ranked setだけを詰める任意の除外 knob だが、canonical rules は節自体を持たず既定 `None` なので gate は無効であり、candidates・full rank・ranked setは同 knob の設定に関わらず動かない。production判断で空売り残/ADVのraw annotationを使うrunは、`margin_short_to_adv`をcore 3 metricと併せて明示する。missing/mismatch/partial cache は `calibration-build --force` で再構築する。保存形式は[`market-lake.md`](./market-lake.md#較正store)を正本とする。

`rules_hash` は `ScreeningRules` の JSON dump 全体から作る。したがって **panel の値を 1 つも変えられない変更（無効な knob の削除・field の並べ替え）でも hash は動き、store 全体が再構築対象になる**。rules model の形を変えるときは、その再構築コストを変更の便益と比べる。

<a id="pre-2019-診断-panel"></a>

## pre-2019診断panel

`--panel-variant pre2019_self_range_375` は self-range を 375 sessions、bar 入力を 600 暦日に固定する診断専用 contract である。通常 store と異なる `--calibration-dir` が必須で、variant と窓は `rules_hash` に含まれ、全 row が `self_range_degraded: true` を持つ。この store を `--run-purpose production_decision` で評価すると拒否する。production panel の既定窓、screening rules、authority 条件は変わらない。

この variant でも各rowの`self_range_observed_sessions`が、その contract の上限へ実際に届いたかを示す。短い履歴をfull-windowとして扱わないための列であり、production self-rangeは750 sessionsのままである。

```bash
uv run baibai-engine screening calibration-build \
  --start 2018-03-01 --end 2019-10-31 \
  --calibration-dir stores/screening/calibration/variants/pre2019 \
  --panel-variant pre2019_self_range_375 --force
uv run baibai-engine screening calibration-evaluate \
  --calibration-dir stores/screening/calibration/variants/pre2019 \
  --horizon 1y --horizon 3y --out /tmp/calibration-pre2019.yaml
```

## store の再構築

旧 CSV store から L2 lake への移行機構は持たない。**旧 store を捨てて全 cohort を再構築する。**
rules が動けば cohort は作り直しになるので、移行を作っても運べるのは「現行 code が読める契約で
書かれた履歴」だけであり、実測ではそれが 0 件だった。

```bash
uv run baibai-engine screening calibration-build \
  --start 2019-11-01 --end <latest-month-end>
```

**実測（2026-08-19、L1 release を名乗る形での全再構築、実 `market.sqlite` 2.0GB）: 81 cohort を
43 分。** forward 1,527,240 行（うち resolved 1,060,478、支配権イベント exit 4,686、破綻型 exit 957）、
closure object 247 件、96.8MB。**同じ履歴が CSV の 507MB から lake の 93MB になる。** bundle manifest は
81 cohort に対して 1,367 bytes である — 世代の cohort inventory は 3 つの dataset manifest から
導出するので、bundle 自体は cohort 数に依存しない。

再構築した cohort は、全datasetが共有したsealed SQLite snapshotのidentityをsourceとして述べる。
calibrationはlake-owned factに加えて`source_coverage`も読むが、L1 releaseはそのledgerを保持せず、
retentionも過去L1 releaseを恒久的なrootにしない。そのためL1 releaseを併記して「同じ入力を将来も
再構築できる」とは主張しない。

```bash
uv run baibai-engine screening calibration-build \
  --start <YYYY-MM-DD> --end <YYYY-MM-DD>
```

logicやrulesを変更した場合は、その時点の完全なmarket storeから全cohortを再構築し、新しい
measurement generationとして評価する。過去入力の完全保存は、現在のproduction method改善に必要な
品質ゲートではなく、2GB storeや別ledger objectを世代ごとに保持する複雑性にも見合わない。

<a id="commands"></a>

## コマンド

```bash
uv run baibai-engine screening backfill-master --month-end-from 2022-09-01 --month-end-to 2026-06-30
uv run baibai-engine screening calibration-build --start 2019-11-01 --end 2026-07-31 --force
uv run baibai-engine screening calibration-evaluate --out .cache/calibration-eval.yaml
uv run python -m tools.experiments.measure_buyback_authorization \
  --out .cache/buyback-authorization-calibration.yaml
uv run baibai-engine screening calibration-evaluate \
  --run-purpose production_decision \
  --required-asof 2021-06-30 \
  --required-metric recommended_rank_top5 \
  --required-metric recommended_rank_top10 \
  --required-metric er_calibration
```

E[r] 水準 parameter を判断する事前登録済み run では、上の core 3 metric に加えて `--required-metric er_level_calibration` を指定する。判断面の月次文脈も更新する run は、同じ command に `--context-out reports/published/er-level-calibration-latest.yaml` を加える。authority が不成立、required cohort が不足、level metric が未解決の場合は context を書かず exit 1 にする。

保持する診断は、selection top-5/top-10のmedian excessとtrap rate、価格収束E[r]の相対較正、FY配当を含むtotal-return E[r]の水準較正、axis/gate/reversionのregression診断、cohortのcoverage/integrityである。これらはtrack recordも統計的有意性も証明しない。

## 改善サイクルの運用契約

基盤（マクロ読み・screening 選定・E[r]/FV/RR 見積り）の改善は独立した運用 loop を持たず、**self-contained issue → 通常の PR delivery** で回す。1 改善 = 1 issue = 1 PR。issue には観察（レポート参照つき）→ 仮説 → 検証方法 → 着手条件と、冒頭に `価値tier: Tn — <因果経路>`（[doctrine](../doctrine.md#improvement-value-hierarchy)）を書く。

### 改善対象マップ（レバーの所在）

| レバー | 所在 | 計測経路 |
| --- | --- | --- |
| screen の閾値・gate・evidence pattern | `method/screening/rules/*.yaml` | 較正リプレイ（rules variant） |
| select の順位付け・diversity cap | 同上 + `engine/src/baibai_engine/screening/selection/` | 較正リプレイ（selection replay） |
| 機械 E[r]・FV アンカー | `engine/src/baibai_engine/screening/estimates.py` | 較正リプレイ（er 軸 IC / decile / 予測 vs 実現） |
| valuation 指標の算出 | metrics 系 + [`valuation-metrics.md`](./valuation-metrics.md) | 較正リプレイ（軸別 IC / coverage） |
| マクロ読みの手順・レンズ | [`macro.md`](./macro.md) + skill `macro-context` | 保有 outcome / 月次の事後検証（N≈1、統計計測はしない） |
| research の見積り手順 | [`thesis.md`](./thesis.md) + skill `research` | portfolio outcome と長期 horizon calibration |
| 資本・cap・sizing | [`portfolio-management.md`](../portfolio-management.md) + `position/policy.py` | 保有 outcome |
| Research Gateの選定判断 | skill `shortlist`の深度契約 | 判断コホート比較（`screening shortlist outcome`）+ 機会費用計測tools |

Evidence Patternを追加・変更・削除するときは、screening rules・対応Research Playbook checklist・selectionの順位・testを同じ変更で整合させ、根拠を較正結果に置く。

### 事前登録と design/confirm

- 採用 judge になる数値基準は**計測を実行する前に** issue または report 冒頭へ書いて commit する（git history が事前登録の正本）。既知の結果がある場合は盲検性の限定を正直に書く。
- cohort を時間で design / confirm に 2 分割し、**両方で同方向・基準充足のときだけ採用**。片側のみは不確定、両側逆は棄却。grid search（基準を後から動かす網羅探索）をしない。

matched 比較の被覆率・membership 数・集中度など、forward outcome を読まずに計算できる sufficiency は、効果条件を凍結する前に実測する。不足する場合は比較設計を修正し、同じ outcome-free 指標を再測定して、あらかじめ定めた sufficiency floor をすべて満たすまで凍結しない。最終設計の実測値と変更点は事前登録 commit に記録する。この修正 loop は forward outcome を一度でも読んだ後には再開せず、凍結後は outcome を見て条件を調整しない。逐次 study は先行 study の効果結果で後続条件を調整せず、match 被覆不足など outcome-free な実行可能性の欠陥は手法上の教訓として後続設計へ適用できる。

- **control cell の判定は「0 許容の全 cell 通過」を既定にしない**（偽陰性へ構造的に偏る）。noise floor（例: trap delta ≤ +2pt）または k-of-n cell 通過と、cell ごとの最小 matched weight を**事前登録で宣言**する。
- 判定語彙は `negative` / `insufficient` / `adoption_candidate` / `inconclusive` の 4 種。同一仮説の再検定は新 evidence（新規満期 cohort・contract レベルの capacity 変更）がある場合に限る。
- **語彙は窓ごとに決めてから全体へ畳む。** 窓を跨いで「どれか 1 つでも被覆不足なら全体 `insufficient`」とすると、1 窓の 1 basis の件数不足が、他窓で確定した効果の不成立を語彙の上で覆い隠す。各窓を `insufficient` / `inconclusive` / `negative` / `adoption_candidate` へ落としたうえで統合し、全体を `insufficient` と呼ぶのは、**効果が確定した窓が 1 つも無い**ときに限る。
- **満期済み窓を根拠に cleanup するときは、残る変動幅を示す。** as-of 範囲が固定で満期済みでも値は不動ではない。forward row は build のたびに再計算され、FY 配当や退場銘柄の exit が backfill されれば `total` basis の pair 数と中央値は動く。したがって次の bullet の `insufficient` 保持規則より削除を優先してよいのは、**その窓の効果が確定しており、かつ窓内の coverage backfill では結論が反転しないことを示した**ときに限る。示せないなら保持規則が優先する。
- `negative` / `inconclusive` が確定した軸は、判定 PR で panel 列・派生計算・評価枝・専用 test を削除し、dated report と git history を反証証跡の正本とする（残すのは `adoption_candidate` / `insufficient` / control 再利用列 / production annotation 入力列のみ）。
- rules variant の計測は本番 rules を変えず `SCREENING_RULES_PATH` で variant を指し、別 store（`stores/screening/calibration/variants/<variant>/`）へ panel を構築する。rules_hash provenance が混線を機械検出する。
- 機械レバー（screen / select / E[r]）の実証的改訂は 3y/5y eligible evidence を必須の関門にし、判断レバー（macro / research 手順）は保有 outcome と運用の事後検証で改める。

### 採用後

- 通過した変更だけを本番へ反映し、計測した構成と本番構成を一致させる。rules 改訂後は panel を `--force` 再構築する。
- 現 asof で `screening run` → `select --review-cap 20` を回し、意図した挙動を実銘柄で確認する（運用テスト）。
- `reports/YYYY-MM-DD-<slug>.md` に再現手順・データ窓・coverage / survivorship 開示・判定表・検算・採用後の監視事項を固定する（一次計測記録。別の監査ファイルは作らない）。マージ前 gate は [`python-foundation.md`](./python-foundation.md) §9 が正本。マージ後は report の監視事項を次の replay 計測で追う。

### 判断コホートの集計

primary-research ticker の research FV と screening FV の bridge は、有効観測（同一 thesis 再実行・scaffold-only・未 review・遡及記入を除く）が 5 件以上になったら乖離率の中央値・範囲・要因件数・coverage を記述集計する。この集計だけで screening 式を変えず、変更仮説は別 issue で事前登録して design/confirm へ進める。四半期ごとに shortlist rejected と assessment reject / defer の `reject_class` 頻度を集計し、機械化可能な型を warning / flag 候補として事前登録する（分類で自動除外・ranking 変更はしない）。

<a id="rejection-cost-preregistration"></a>

#### 棄却のコストに関する事前登録

`screening shortlist outcome` は selected / rejected / machine top-N に加えて、rejected を `reject_class` 別に集計する（`rejected_by_class`）。**次の判定基準を計測の実行前にここへ固定する。**

- **3m / 6m は alert のみ**。手順・閾値の変更根拠にしない（doctrine 柱 5）。
- **手順変更の検討に進む条件**: 1y 以上の horizon で、cohort 数 8 以上・rejected の中央超過が selected の中央超過を上回る状態が、時間で 2 分割した両期間に同方向で出ること。片側のみは `inconclusive` とする。
- **`reject_class` 別の解釈**: 母数が 10 件未満の class は中央超過を算出せず件数だけを並べる。特定の class が上の条件を満たした場合に限り、その class の判定手順を見直す issue を起票する。分類そのものを自動除外・ranking へ入れることはしない。
- **深掘りまで進んで棄却した lane**（bargain assessment の reject / defer）は母数が桁で少ないので、統計ではなく個票で追う。`baibai_engine.research_watch` が研究 FV と現在価格の位置を毎営業日出すので、価格が研究 FV を下回った lane を再評価の入口にする。
- **基準を後から動かさない**。動かす場合は、動かしたことと理由を次の dated report に明記する。

初回の採点可能日は 2026-10-17（最古 shortlist 2026-07-17 + 3m）である。

<a id="catalyst-axis-preregistration"></a>

#### カタリスト軸の事前登録

`screening shortlist outcome`はselectedを、Research Gate narrativeが日付つきカタリストを持つか否かで2分する（`selected_by_catalyst`）。**次を計測の実行前に固定する。**

- **切る場所は selected の内側だけ**。rejected は narrative を持たないので、pool 全体で切ると selected / rejected の差をカタリストの差として報告することになる。
- **判定基準は棄却コストと同じ**（1y 以上・cohort 8 以上・時間 2 分割で同方向）。満たすまで方向を主張しない。
- **母数が 10 件未満の側は中央超過を算出せず件数だけを並べる。**
- **この軸は選定にもrankingにも入れない。** 満たした場合に起票できるのはResearch Gate深度契約（カタリストの日付要求）の見直しだけであり、`tse_capital_policy_status`などのannotationを機械の入力へ昇格させる根拠にはしない。

初回の採点可能日は棄却コストと同じ 2026-10-17 である。

### 支配権イベントの実現 exit 値

上場廃止で市場終値が無くなった forward 窓は、成立した現金公開買付けの 1 株買付価格で解決する（`resolved_control_event_exit`）。これは効果量を選ぶ仮説ではなく、観測済みの対価へ置き換える correctness 変更である。導出規則・置換規則・比較方法・停止条件は [`reports/studies/2026-08-11-capital-control-exit-values/preregistration.md`](../../reports/studies/2026-08-11-capital-control-exit-values/preregistration.md) に事前登録し、置換前後の較正影響を同ディレクトリの report に固定する。実値化できない上場廃止は従来どおり全損・中立の両側 bracket に残り、`unpriced_exit_flips_direction` の判定材料であり続ける。

### 破綻型の実現 exit 値

上場維持基準への不適合・破産・民事再生・会社更生・債務超過・内部管理体制・開示義務違反による上場廃止は、市場が最後に付けた終値で解決する（`resolved_failure_exit`）。資金が消えた退出には再投資の問いが立たないので、買収型と違い対価の規約を決めずに実値化できる。

分類は JPX が `jpx_delistings.reason` に自由記述で書く語で行い、**fail-closed** とする。買収を示す語（完全子会社化・買収・公開買付・株式等売渡請求・合併・ＭＢＯ・株式移転・株式交換）を含む reason は破綻型としない。買収を破綻型と読むと買収プレミアムを全損として記録するのに対し、破綻型を分類し損ねても既存の欠落が残るだけである。`株式の併合` 単独はスクイーズアウトの第 2 段階なので破綻型ではない。

価格は entry と同じ調整系列の終値どうしなので、買付価格と違い基準の突合を要さない。調整 factor の被覆は `resolved` と同じく行に記録し gate はしない。退出日が上場廃止日より後の銘柄（廃止後に再び取引された系列）と、1 窓に破綻型の廃止が 2 件入る銘柄は実値化せず unresolved に残す。廃止のかなり前に売買停止された銘柄は停止前の終値で評価されるため損失を過小に測るが、これは行ごと落とす現状と同じ向きで、より小さい。

### 誠実性の規律

1. 有意性・統計的優位を主張しない。効果量と cohort 勝率で判断し、そう書く。
2. 仮説と採否基準は検証前に事前登録し、後から動かさない。
3. survivorship・coverage の欠け・レジーム文脈を計数で開示する。
4. 累積リターン・年率・シャープ等を track record として掲げない。
5. post-hoc の判断はそう明記する。
