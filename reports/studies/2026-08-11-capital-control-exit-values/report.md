---
title: "支配権イベントの実現 exit 値置換 — 計測記録"
summary: "成立した現金公開買付けの買付価格で上場廃止銘柄の未解決 forward return を実値化し、置換前後の較正影響を固定する。"
doc_type: measurement-record
status: active
date: 2026-08-11
---

# 支配権イベントの実現 exit 値置換 — 計測記録

価値tier: T1 — 上場廃止で観測不能になった長期 return を成立済み公開買付けの現金対価で実値化し、買収プレミアムを全損・中立代入へ落とす較正誤差を減らす。

事前登録は [`preregistration.md`](./preregistration.md)。置換規則・比較方法・停止条件はそこで固定済みで、本書は実行結果だけを載せる。機械 artifact は [`comparison.yaml`](./comparison.yaml)。

## 結論

**verdict: `adopted`（correctness 変更として採用）**

満期済み cohort の未解決 exit のうち 4,686 行（147 ticker、各 horizon 19 cohort）を、成立した現金公開買付けの買付価格で実値化した。市場終値で閉じた行は 1 行も上書きしていない。panel・E[r]・selection rank は変わらず、core metric 3 本の結論方向も変わらない。E[r] 較正の平均絶対誤差は 3y 0.126693 → 0.126282、5y 0.194769 → 0.191632 と縮んだ。

authority の判定は動かない。eligible cohort は 3y 34 / 5y 18 で前後同数、`unpriced_exit_flips_direction` も 3y 5 件のまま残る。**実値化しても方向感度 blocker は解消しない** — 残った未解決行のほうが桁で多いためで、これは事前登録が採否閾値にしないと明記した性質の量である。

## 誠実性境界と provenance

順序を次で固定した。

1. 事前登録を commit `ec9b0816` で確定
2. その後に source coverage（EDINET 様式別件数・対象 ticker 解決率・JPX 上場廃止件数）を確認
3. 実装と実データ導出を行い、置換前後の較正 metric は最後に 1 度だけ読んだ
4. 独立レビューが出した fail-open を commit `b4a4ba47` で塞いだ。いずれも判定基準ではなく、実値化の可否を厳しくする方向の修正である

置換規則・比較対象・core metric・停止条件は結果を見てから動かしていない。重複する月次 cohort は独立標本ではなく、有意性・track record を主張しない。

| artifact | SHA-256 |
| --- | --- |
| `preregistration.md` | `4bfc5347bf81372cf09293a9eaf446d0c9cde2dc0b632ca21510ef93ce4cfbbf` |
| `comparison.yaml` | `d04ca29f5bdea34ed5ed628d0beab6a1fa4f443b186f4a1ede4cd15cb2e26c16` |
| baseline evaluation | `bf10f5f3f0a90f418e83c3e1154da11e43024ad7c58491a0cbf5400ae9280152` |
| actual-exit evaluation | `05d3424b22e03f4b89774dae09f429728ef1f057de6d0e68c3c764926ea95cd6` |
| market store | `cc29257fd526f581f7700885e92d62cb4186d663063ba5c5a7d6ec5a5177fc1f` |

identity: screening rules hash `f4a3f3f20838e1cd`、calibration cache schema 15、E[r] model `expected-return-v1`、cohort 窓 2019-11-29〜2026-06-30 の 80 cohort。

## 再現手順

```bash
uv run baibai-engine screening refresh-capital-control --asof 2026-08-10
uv run baibai-engine screening build-control-event-exits --asof 2026-08-10
uv run baibai-engine screening calibration-build --start 2019-11-01 --end 2026-06-30 --force
# baseline は同じ panel を再利用し forward だけ組み直す
cp stores/screening/calibration/panel-*.csv stores/screening/calibration/panel-*.meta.yaml \
   stores/screening/calibration/calibration.meta.yaml stores/screening/calibration-baseline/
uv run baibai-engine screening calibration-build --start 2019-11-01 --end 2026-06-30 \
  --calibration-dir stores/screening/calibration-baseline --without-control-event-exits
uv run baibai-engine screening calibration-evaluate --calibration-dir stores/screening/calibration-baseline --out /tmp/evaluation-baseline.yaml
uv run baibai-engine screening calibration-evaluate --out /tmp/evaluation-actual.yaml
uv run python -m tools.experiments.measure_control_event_exits \
  --baseline-dir stores/screening/calibration-baseline --actual-dir stores/screening/calibration \
  --baseline-evaluation /tmp/evaluation-baseline.yaml --actual-evaluation /tmp/evaluation-actual.yaml \
  --out reports/studies/2026-08-11-capital-control-exit-values/comparison.yaml
```

`--force` 再構築の実測は 53 分 44 秒（panel 80 件 42 分、forward 150 万行 11 分）。baseline は panel を再利用するので forward 分だけで済む。

## 1. Source coverage

| source | 値 |
| --- | --- |
| JPX 上場廃止行 | 760（うち 2026-08-10 以前 744） |
| 上場廃止理由が公開買付けを名指す行 | 163（うち 2026-08-10 以前 160） |
| EDINET 様式別行数 | 240: 315 / 250: 304 / 260: 1 / 270: 318 / 280: 1 / 350: 27,953 / 360: 4,706 |
| 東証開示企業一覧 | 14 か月分 32,284 行（2025-05-31〜2026-06-30） |

EDINET の識別列を保持窓（2024-07-31〜2026-08-10、741 日 168,837 件）へ backfill した結果、公開買付書類 938 行のうち 900 行で対象会社を解決でき、**上場廃止 160 件は全件で対象 ticker を解決できた**。事前登録 §2 が固定した「同じ EDINET 一覧履歴で観測した `(edinetCode, secCode)` 対応へ結合する」方法だけで足りており、外部の code list を持ち込む必要はなかった。

issue 本文は開示企業一覧が 2024-01 以降の月次スナップショットを内蔵すると記していたが、実物は 14 か月分（2025-05 以降）だった。PIT 展開の起点はその最古シートであり、そこに既に載っていた 28,365 行は初回開示月を特定できないので left-censored として保持する。

## 2. 実現 exit 値の導出

160 件の対象のうち **152 件を実値化した**。棄却 8 件の内訳は次のとおりで、いずれも事前登録 §3 が「推定しない」と決めた形である。

| 棄却理由 | 件数 | 内容 |
| --- | --- | --- |
| `multiple_offerors` | 4 | 同一対象へ複数の提出者が届出。買収合戦および先行 TOB のあとの別提出者による TOB |
| `price_or_outcome_unreadable` | 4 | 同一提出者の二段階公開買付け（応募合意株主向けと少数株主向けで価格が違う） |

二段階案件は導出中に実データが教えた形である。9675 常磐興産では第二回公開買付価格 1,240 円を採ると最終終値 1,643 円に対し比 0.755 となり、少数株主の実現 exit ではないことが数値で分かった。届出書 chain 単位で価格を読み、chain 間で価格が一致しない案件は実値化しない規則へ変えた。

### 独立検算（AP-02）

152 件すべてについて、買付価格を上場廃止日以前の最終終値と突き合わせた。

| 統計 | 値 |
| --- | --- |
| 買付価格 / 最終終値 の最小 | 1.0000 |
| 同 中央値 | 1.0046 |
| 同 最大 | 1.0125 |
| `[0.95, 1.10]` の外 | 0 件 |
| 上場廃止日より後に株式調整 event を持つ ticker | 0 件 |
| 上場廃止日より後に bar を持つ ticker | 0 件 |

成立した現金公開買付けでは買付期間中の市場価格が買付価格のわずか下に張り付くので、この帯は一次資料と独立に整合する。読み違えがあれば外れ値として現れる。

3 件（8283 ＰＡＬＴＡＣ、6197 ソラスト、3271 ＴＨＥグローバル社）は届出書の注記に載る対象者名・買付価格・資金表の「金銭以外の対価の種類―」・報告書の成否文言・最終終値を目視で突合し、すべて一致することを確認した。

株式基準の guard（上場廃止日より後の累積 `adjustment_factor` が 1.0 でなければ実値化しない）は、実データでは 1 件も発火しなかった。上場廃止後に bar が無いためで、この guard が効くのは ticker 再利用の場合だけである。

## 3. forward の置換

| horizon | 置換行 | 置換前 status | 影響 cohort | 未解決行 before → after |
| --- | --- | --- | --- | --- |
| 3m | 340 | `unresolved_stale_exit` | 19 | 8,797 → 8,457 |
| 6m | 685 | 同 | 19 | 21,648 → 20,963 |
| 1y | 1,147 | 同 | 19 | 47,027 → 45,880 |
| 3y | 1,289 | 同 | 19 | 143,970 → 142,681 |
| 5y | 1,225 | 同 | 19 | 232,518 → 231,293 |

合計 4,686 行、147 ticker。**置換規則の外で変わった行は 0 件**（`rows_changed_outside_the_replacement_rule: []`）。置換前 status は全件 `unresolved_stale_exit` で、`unresolved_missing_exit` からの置換は無かった — 上場廃止銘柄は廃止直前まで取引されるので、目標日より前に必ず最後の bar が残るためである。

exit 値 152 件のうち 147 件だけが使われた。残る 5 件（7317 / 5903 / 3271 / 6197 / 8283）は上場廃止日が 2026-07-17〜2026-08-10 で、最終 cohort as-of 2026-06-30 の 3m 窓がまだ満期を迎えていない。cohort が進めば使われる。

## 4. authority への影響

| | baseline | actual-exit |
| --- | --- | --- |
| eligible cohort 3y | 34 | 34 |
| eligible cohort 5y | 18 | 18 |
| eligible metric cohort | 52 | 52 |
| blocked / unresolved | 108 | 108 |
| 3y `unpriced_exit_flips_direction` | 5 | 5 |
| 3y `entry_price_gap` | 6 | 6 |
| 5y `entry_price_gap` | 6 | 6 |

**authority は 1 つも動かない。** 3y で 1,289 行を実値化しても、その cohort に残る未解決 exit は 142,681 行あり、全損・中立の両側代入が結論の符号を動かす cohort は 5 件のまま残る。実値化は survivorship の穴を塞ぐが、塞いだ割合は 0.9% であって blocker を外す量ではない。

## 5. core metric

| horizon | metric | baseline | actual-exit | 方向 |
| --- | --- | --- | --- | --- |
| 3y | `recommended_rank_top5` mean median excess | 0.404641 | 0.397758 | 正のまま |
| 3y | `recommended_rank_top10` mean median excess | 0.348468 | 0.346752 | 正のまま |
| 3y | `er_calibration` 平均絶対較正誤差 | 0.126693 | **0.126282** | 縮小 |
| 3y | `er_calibration` top-bottom spread | 0.578699 | 0.579674 | 正のまま |
| 5y | `recommended_rank_top5` mean median excess | 0.605596 | 0.601271 | 正のまま |
| 5y | `recommended_rank_top10` mean median excess | 0.625161 | 0.620837 | 正のまま |
| 5y | `er_calibration` 平均絶対較正誤差 | 0.194769 | **0.191632** | 縮小 |
| 5y | `er_calibration` top-bottom spread | 0.997965 | 0.993937 | 正のまま |

`spread_positive_share` は両 horizon 両契約で 1.0。E[r] quintile の観測数は 3y 54,078 → 54,570（+492）、5y 22,593 → 23,043（+450）。

selection cohort の平均 n は 3y で 4.8 → 4.9、9.6 → 9.7 と増え、中央超過はわずかに下がる。**買収された銘柄の実現 return は推奨上位の中央値より低かった**ということであり、その方向は事前に決めていない。中立代入が上限にならないという事前登録の懸念は、少なくともこの母集団では逆向きに出ている。効果量は 0.7pt 未満で、cohort 数と重複窓を踏まえれば方向を主張できる量ではない。

## 6. integrity

- panel 80 件は baseline / actual-exit で SHA-256 完全一致（`panels_identical: true`）。E[r]・selection rank・gate は panel から決まるので、これらが動いていないことの証明になる。
- 参考として、cache schema 14 で作られていた再構築前の store とも比較した。現行契約が読む全 column の全 cell が一致し、差分は現行 `PanelRow` が持たない `er_upside_raw` column の有無だけだった（store reader が「契約が読まなくなった column は無視する」と定めている通りの状態）。**再構築は panel 値を 1 つも動かしていない。**
- forward 行は置換した 4,686 行以外すべて一致。置換行が変えた field も `resolved` / `price_return` / `exit_date` / `status` / `stale_price` / 配当 3 field に閉じている。

## 7. 検算

1. horizon 別置換行の合計 340 + 685 + 1,147 + 1,289 + 1,225 = 4,686。build 出力の `control event exits=4686` と一致。
2. 未解決行の減少 (8,797−8,457) + (21,648−20,963) + (47,027−45,880) + (143,970−142,681) + (232,518−231,293) = 340 + 685 + 1,147 + 1,289 + 1,225 = 4,686。置換以外の経路で未解決が動いていない。
3. resolved 行数 1,059,946（actual）− 1,055,260（baseline）= 4,686。forward 総行数は両者 1,509,220 で同一。
4. exit 値 152 − 使用 147 = 5。5 件の上場廃止日はいずれも最終 cohort as-of より後で、窓が満期に達していないことを個別に確認した。

## 8. 取込の冪等性

同じ command を 2 回実行して結果が変わらないことを実データで確認した。

| command | 1 回目 | 2 回目 |
| --- | --- | --- |
| `build-control-event-exits --asof 2026-08-10` | 152 行 / 買付価格合計 ¥443,538 | 同一 |
| `refresh-capital-control --asof 2026-08-10` | 32,284 行 / 14 か月 / 上場廃止 760 行 | 同一 |
| `backfill-edinet-identity --start 2026-08-01 --end 2026-08-10` | 2,171 件 / list 10 日 | 同一 |

## 9. 解釈の限界

事前登録 §7 を再掲し、実データで確認した範囲を加える。

- 実値化できるのは**成立した現金公開買付けだけ**である。実データでは上場廃止 744 件のうち理由が公開買付けを名指すのは 160 件で、残りは株式の併合・株式等売渡請求だけで完結しており、買付価格を持つ一次資料に辿り着けない。それらは従来どおり bracket に残る。
- JPX の上場廃止理由の語彙は 2024 年以降に変わっている。2025 年より前の廃止は「株式の併合」「株式等売渡請求による取得」としか書かれておらず、背後に公開買付けがあったかを理由文から判定できない。EDINET の保持窓（2024-07〜）も同じ時期から始まるので、**実値化は事実上 2025 年以降の上場廃止に限られる**。
- 公開買付価格は市場での売却時点や税・手数料を表さない。短期 event-driven return を測るものではない。
- 重なり合う月次 cohort は独立標本ではない。有意性・統計的優位・track record を主張しない。
- candidate annotation の存在は企業品質・将来リターン・買収確率を意味しない。

## 10. 監視事項

- **cohort が進むたびに再導出する。** `build-control-event-exits` は月次 ops 手順に入れた。未使用の 5 件は次の cohort 追加で窓に入る。
- **`unpriced_exit_flips_direction` は 3y 5 件のまま。** 実値化では解消しないので、この blocker を外したいなら別の証拠（廃止時の対価が資料に無い案件の扱い）が要る。
- **上場廃止後の再上場が 2 件ある**（8729 ソニーフィナンシャル 2025-09-29、8303 ＳＢＩ新生銀行 2025-12-17）。同じ ticker の bar 系列が廃止前後で連続してしまうため、`asof_basis_closes` が最終 bar 基準へ揃える際に廃止前の close が再上場後の株式調整で歪む可能性がある。今回の exit 値 152 件はいずれもこの 2 件を含まないので本計測には影響しないが、較正一般の既知の穴として残る。
- **selection cohort の中央超過がわずかに下がった向き**は、cohort が増えたところで再確認する。今回の差は効果量として読まない。
