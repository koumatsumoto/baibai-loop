---
title: "E[r] 実現分布 context v2 と cap 2 点検証 — 事前登録"
summary: "判断面へ出す固定 band の実現分布と、upside / buyback cap の固定2点リプレイ契約を結果計測前に固定する。"
doc_type: measurement-record
status: active
date: 2026-08-10
---

# E[r] 実現分布 context v2 と cap 2 点検証 — 事前登録

価値tier: T1 — 機械 E[r] の帯ごとの下方分布を判断面へ供給し、割安候補の過剰棄却と value trap の見落としを減らす。

## 1. 既知情報と計測境界

既知の v1 較正では、E[r] 最上位 quintile の予測値に対して実現 total return が 3y で +11.99pt、5y で +10.94pt 上振れし、最下位 quintile は逆に下振れした。現行 context は 11 cohort と旧 `screening_rules_hash` に束縛され、判断面では非表示になっている。満期済み cohort の integrity 改善後に core metric と `er_level_calibration` が eligible になる母数は 3y 39、5y 18 と既知である。

本書を commit するまで、`UPSIDE_CAP = 1.00` または `BUYBACK_CLIP = 0.10` で再構成した E[r] の forward outcome、band v2 の実現分布、窓別 gate、verdict を計測しない。既存 panel / forward schema、現行 E[r] 式、authority status、旧 v1 artifact の値は確認してよい。

context v2 は cap 仮説の結果に依存しない成果物である。cap variant が不採用でも、現行 production E[r] の band 分布を出荷する。production の `UPSIDE_CAP`、`BUYBACK_CLIP`、E[r]、FV、rank、gate は本 study で変更しない。

## 2. context v2 の固定集計契約

### 2.1 母集団と band

各 horizon で、現行 authority が core 3 metric と `er_level_calibration` をすべて `eligible` と判定する満期済み cohort を全件使う。結果を見て cohort を除外しない。主読みは horizon ごとの全 eligible cohort、比較読みは 3y / 5y の両方が eligible な共通 as-of 窓とする。

各 cohort の `in_population`、`total_return_status == resolved`、現行 `er_annual` が有限な ticker observation を `er_annual` 昇順の quintile に分け、`q1`〜`q5` とする。同値境界は既存 `er_level_calibration` と同じ割当を使う。これと重複可能な固定 band `er_gte_8_5pct` を `er_annual >= 0.085` で作る。8.5% band は Q5 の置換ではなく、人間統治の hurdle と実現分布を対応づける独立集計である。

表示用の quintile 上端は、cohort ごとの上端を cohort 等重みで集計した中央値とする。判断面の run を band に対応づけるときは、この固定表示上端を昇順に適用する。8.5% band は上端を持たず、下端 `0.085` を明示する。

### 2.2 basis、重み、統計量

主 basis は `fy_actual_dividend_total_return`、副 basis は `price_return_only` とする。`median` / `q25` / `q10` は各 row の実現 return を nominal horizon で年率化した絶対年率であり、「E[r] 8.5% が歴史的にどの実現年率へ対応したか」を直接読める座標にする。trap は各 basis の cumulative return から同一 cohort・同一 basis の流動性母集団中央値を引いた excess が `<= -0.20` の row とする。TOPIX return は補助 provenanceとして保持するが、band分位またはtrapの基準へ混ぜない。

band × horizon × basis ごとに次を固定する。

- 主読み `ticker_equal`: 全 ticker-as-of observation を等重みにした実現絶対年率の `median`、`q25`、`q10`、trap率、`n`。
- 副読み `cohort_equal`: cohort ごとに同じ5統計量を作り、その値を cohort 等重みで集計した中央値。trap率も cohort trap率の中央値とする。
- coverage: `cohort_count`、cohort内 `n` の中央値 `median_n`、as-of 範囲。

月次 cohort は窓が重複するため、ticker-as-of observation を独立標本とは呼ばず、有意性や track record を主張しない。分位点は線形補間せず、既存評価で使う deterministic nearest-rank 規則へ統一する。

## 3. 固定する cap variant

現行式を control とし、次の2 variantだけを個別に比較する。両方を同時変更した variant、中間値、追加点を作らない。

| 仮説 | control | variant | variant E[r] |
| --- | --- | --- | --- |
| H-1 upside cap | `UPSIDE_CAP = 0.50` | `UPSIDE_CAP = 1.00` | `0.10 * clip(raw_upside_blend, +/-1.00) + current_carry` |
| H-2 buyback clip | `BUYBACK_CLIP = 0.05` | `BUYBACK_CLIP = 0.10` | `current_reversion + dividend_yield + clip(-net_share_change_yoy, +/-0.10)` |

H-1 の `raw_upside_blend` は production の `estimate_expected_return` が cap 前に計算する同じ値を panel に保存する。H-2 の欠損 dividend / buyback は production と同じ 0 寄せを使う。variant は評価時だけ再構成し、production model を呼び替えない。

H-3 は診断だけとする。`raw_upside_blend < 0` と `>= 0` に分け、`realized_reversion_rate = annualized(price_return) / raw_upside_blend` の median / q25 / q10 と符号一致率を出す。分母の絶対値が 0.05 未満の row と非有限値は除外し、採否語彙へ接続しない。

## 4. 時間分割と採否 gate

各 horizon の eligible cohort を as-of 昇順に並べ、前半を design、後半を confirm とする。奇数件は confirm 側を1件多くする。既知母数どおりなら 3y は 19 / 20、5y は 9 / 9 になる。eligible status は outcome値を読まない integrity contractで確定し、分割後に cohort を移動しない。

各 H-1 / H-2 を独立に、3y-design、3y-confirm、5y-design、5y-confirm の4窓で判定する。各窓では次をすべて満たすことを要求する。

1. **水準 MAE**: cohortごとに、resolved total-return rowの `abs(annualized(total_return) - variant_er)` 中央値を計算する。cohort等重みの中央値が control より **0.005/年（0.50pt）以上改善**する。
2. **cohort改善率**: variant MAE が control MAE より小さい cohort が **2/3以上**。同値は改善に数えない。
3. **top-N順位非劣化**: `pass_screen` × variant E[r] non-null集合を variant E[r] 降順に並べた top-20 / top-5 の cohort median excessを作る。cohort等重み中央値が、同じ集合を control E[r] 順にした値より **-0.01（-1pt）以上**である。
4. **trap非劣化**: top-20 / top-5 の ticker等重み trap率が control より **+0.01（+1pt）を超えて悪化しない**。
5. **coverage**: 各cohortのlevel MAE対象が100件以上、各top-Nが要求Nを満たし、authority / basis statusがeligibleである。

窓別 verdict は次で固定する。

- `adoption_candidate`: coverageを満たし、5 gateをすべて通過。
- `negative`: coverageを満たすが、MAE改善、cohort改善率、順位、trapのいずれかが閾値と逆方向または不通過。
- `insufficient`: coverage不足で効果を判定できない。
- `inconclusive`: coverageは満たすが、境界値・数値不安定など上記で一意に畳めない場合。

全体 `adoption_candidate` は4窓すべてが `adoption_candidate` の場合だけとする。効果を判定できた窓に `negative` が1つでもあれば全体 `negative`、`negative` がなく不足窓だけなら `insufficient`、それ以外は `inconclusive` とする。採用候補でも production parameter 変更は別Issueとし、`production_change_allowed` を改めて要求する。

## 5. artifact、失効防止、cleanup

context v2 artifact は現行 `screening_rules_hash` / `er_model_version`、生成時刻、45日expiry、schema versionを持つ。生成元 evaluation artifact の SHA-256 と context artifact 自身の SHA-256をdated reportへ固定する。loaderはmethod identity、schema、basis、band、分位順、as-of、expiryの不整合をfail closedで非表示にする。

rules hash が変わる変更では、同じCIでproduction method identityとpublished context identityを比較し、不一致なら失敗させる。運用側は月次更新で再生成する。これにより、hash不一致を7日超放置する経路を持たない。

H-1 / H-2 が `negative` または `inconclusive` なら専用variant計算・専用testを通常treeから削除する。context v2生成に必要な共通集計は残す。raw upside panel列は H-1 の判定後、判断面または次の事前登録済みstudyで具体的な消費経路がなければ削除する。結果reportとhashは反証記録として残す。

## 6. 検算と回帰

- 合成fixtureで band境界、8.5%包含、nearest-rankのmedian/q25/q10、trap境界 `-0.20`、ticker/cohort重みを手計算と一致させる。
- 代表する3y / 5y bandのmedian/q10/trapを、context生成moduleをimportしない独立計算で再現する。
- H-1/H-2は2点以外を受理しない。H-1とH-2の同時変更を拒否する。
- production `estimates.py` の定数、現行candidate E[r]、FV、selection rankが不変であることを回帰testで固定する。
- full gate（Ruff format/check、mypy、pytest、import-linter、bandit）を通す。
