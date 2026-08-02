---
title: "株数減少・予想増配の長期evidence事前登録"
summary: "既知のintegrity blockerをGate 0に置き、2成分の長期診断と一次IR固定標本を分離する。"
doc_type: report
status: active
date: 2026-08-02
---

# 株数減少・予想増配の長期evidence事前登録

価値tier: T1 — 低valuation候補のバリュートラップ率を下げる還元変化のうち、再現可能で一次情報に裏付けられた成分だけを判断面へ進める。

本書は #737 の独立3y confirm / 5y component outcomeと一次IR分類を読む前に、仮説、cohort、標本、control、採否条件を固定する。#718 / #736 の1y design / confirmと3y design、および #738 で判明した独立3y cohortのgeneric integrityは既知情報として明示する。未知だったものとして扱わない。

## 1. 仮説とproduction境界

2成分を別仮説として扱い、compositeで相互補完しない。

- H-S: `share_count_reduction_streak >= 1` の低PER候補は、`== 0` より将来price-only excessが高くtrapが少ない。
- H-D: `dps_guidance_up == true` の低PER候補は、`false` より将来price-only excessが高くtrapが少ない。

H-Sは分割・併合を補正した自己株式込みグロス発行済株式数の純減proxyであり、自己株取得の直接観測ではない。H-Dは最新実績disclosure後の最新split-safe予想DPSが最新実績DPSを上回るかであり、実現増配やtotal shareholder returnではない。

採用候補となり得るsurfaceはnullableなcandidate annotationだけとする。H-Sは`発行済株式数減少`、H-Dは`予想DPS増`と表示し、一次IR分類が良くても`自社株買い`へ名前を変えない。gate、ranking、FV、E[r]、sizingは対象外である。

## 2. Gate 0: maturityとauthority

独立3y confirmは、#718の3y design最終entry `2020-05-29` より後で、2026-07-31までに満期した次の2 cohortへ固定する。

- `2023-06-30`
- `2023-07-31`

固定3y/5y authorityは、結果を見る前に別production検定で選択済みの4 as-ofを再利用する。

- `2020-01-31`
- `2020-05-29`
- `2021-01-29`
- `2021-05-31`

既存production authorityのcore 3 metricを使い、独立confirmの2 cohortが両方integrity eligible、固定4 as-of × 3y/5yが8組すべてeligibleであることをGate 0とする。#738で、独立confirmはpriced-master対象forward returnの未解決により2 cohortともintegrity blockedと既知である。この状態が同じ入力で再現すれば、H-S / H-Dは効果方向にかかわらず`insufficient`とし、production変更を行わない。

integrity blockerを個別componentの良い方向、delisting代入での方向安定、固定長期の良い結果で上書きしない。別cohortへの差し替えもしない。

## 3. component診断

Gate 0の成否と独立に、原因を記録するため同じ固定cohortのcomponent outcomeを診断する。metricはresolvedな流動性母集団のmedian price returnに対する`price_return_only` excess、trapは`excess < -0.20`とする。

各cohortで`in_population`、resolved forward、正の`per_trailing`を満たす行をPER昇順・ticker昇順に並べ、先頭20%をlow valuation bandとする。H-Sはstreak 1/2をtrue、0をfalse、nullを除外する。H-Dは既存boolのtrue / falseを使い、nullを除外する。

固定出力はcomponentごとに次を持つ。

- low valuation band n、eligible n
- true / falseのn、median excess、mean excess、trap rate
- true−falseのmedian / mean / trap delta
- cohort横断の単純平均、median delta positive share、両群合計n
- 6 controlのavailable cohort、matched weight合計、mean stratified median delta、mean stratified trap delta

controlは #718 と同じ順序で、`price_change_60d`を必須のまま残す。

1. `dividend_yield`
2. `per_trailing`
3. `pbr`
4. `market_cap_oku`
5. `avg_turnover_oku`
6. `price_change_60d`

各controlはcohort内中央値の上下へ独立に二分し、各stratumでtrue / falseが5行以上ある場合だけ使う。重みは`min(true_n, false_n)`で、交差strataは作らない。

Gate 0が通った場合にだけ、各仮説を次の効果条件で`adoption_candidate`とする。

### 独立3y confirm

- 2 cohortともdelta算出可能
- mean median delta `>= 0.03`、positive cohort share `= 100%`、mean trap delta `<= 0`
- 全6 controlが2 cohortとも利用可能、mean median delta `> 0`、mean trap delta `<= 0`
- H-Sはtrue合計40 / false合計300以上、H-Dはtrue合計150 / false合計180以上
- 各controlのmatched weight合計20以上

### 固定3y/5y

- horizonごとに4 cohortともdelta算出可能
- mean median delta `>= 0.03`、positive cohort share `>= 75%`、mean trap delta `<= 0`
- 全6 controlが4 cohortとも利用可能、mean median delta `> 0`、mean trap delta `<= 0`
- H-Sはtrue合計50 / false合計500以上、H-Dはtrue合計150 / false合計300以上
- 各controlのmatched weight合計30以上

方向・効果量・controlが外れた成分は`negative`、Gate 0または標本だけが外れた成分は`insufficient`とする。良いhorizon、control、成分だけで上書きしない。有意性検定、p値、重複monthly cohortの独立標本扱いは行わない。

## 4. 株数減少proxyの一次IR固定標本

forward outcomeと独立にproxyの意味を確認する。`2025-06-30` production panelのlow valuation band 261行のうち、`share_count_reduction_streak >= 1` の50行を母集団とする。`sha256("2025-06-30:" + ticker)`の昇順、同値はticker昇順で先頭12社を固定標本とする。

| ticker | company | streak |
| --- | --- | ---: |
| 8395 | 佐賀銀行 | 1 |
| 5741 | UACJ | 1 |
| 5186 | ニッタ | 1 |
| 9107 | 川崎汽船 | 2 |
| 6326 | クボタ | 2 |
| 8377 | ほくほくフィナンシャルグループ | 2 |
| 3443 | 川田テクノロジーズ | 1 |
| 7270 | SUBARU | 2 |
| 7246 | プレス工業 | 2 |
| 4202 | ダイセル | 2 |
| 7202 | いすゞ自動車 | 1 |
| 1885 | 東亜建設工業 | 1 |

最新側のFY-to-FY減少1件を、会社IR、法定開示、取引所開示の一次資料から次へ分類する。

1. 自己株取得と消却の明示的な連鎖
2. 自己株消却（取得との連鎖を標本資料だけで確定できない）
3. 増資・株式交付との純額
4. 合併、株式交換、会社分割等の組織再編
5. その他
6. unresolved

資料に自己株取得があるだけでは1にせず、同じ減少対象株式の消却との対応を要求する。自己株取得はgross sharesを直接減らさないためである。分割・併合だけで生じる見かけの変化はpanel正規化不良として別記し、分類割合の分母へ入れない。

H-Sをproduction候補にする追加条件は、12社中10社以上をclassifyでき、分類1+2がclassifiable標本の70%以上、unresolvedが2社以下であることとする。この条件を外せば、outcomeが良くても`発行済株式数減少`以上の意味を判断面へ持ち込まずH-Sを`negative`とする。

## 5. 実装・検算境界

- 単発診断は`tools/`のread-only scriptから始め、stable CLI、panel/cache schema、production candidateへ昇格させない。
- scriptとfixtureをoutcome計測前にcommitし、その後にartifactを生成してSHA-256を記録する。
- 代表cohortをpanel / forward CSVから別計算し、low valuation境界、true / false n、median、trap、`price_change_60d` controlを検算する。
- 一次IRはURL、開示日、対象株数、分類根拠を会社ごとに記録し、検索結果snippetや二次情報だけで分類しない。
- Gate 0がblockedならannotation issueを作らず、`insufficient` outcomeとして閉じる。
