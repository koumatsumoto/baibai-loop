---
title: "較正 evidence 容量拡張の事前登録"
summary: "priced master 欠損の有界バイアス判定と、375-session self-range を使う pre-2019 診断 panel の規則・評価量・採否条件を計測前に固定する。"
doc_type: measurement-record
status: active
---

# 較正 evidence 容量拡張 — 事前登録

価値tier: T2 — 3y/5y production evidence の eligible cohort と市場局面の幅を増やし、後続する T1 の較正判断が少数 cohort の選び方に依存する余地を減らす。

## 0. 既知の観察と計測前の境界

既存 report から、3y は満期済み 44 cohort 中 5、5y は 20 中 4 だけが全 blocker を通る。`priced_master_without_universe` は 3y 38 / 5y 16 cohort を block し、cohort ごとの該当数は 1〜31、流動性母集団比は概ね 3% 以下である。この件数と blocker 内訳は既知であり、本事前登録は盲検ではない。

本書を commit するまで、変更後の coverage 判定も pre-2019 変種 panel も計測しない。実装上の契約確認と既存 report の数値確認だけを行う。

## 1. WS-1: `priced_master_without_universe` の有界バイアス判定

### 1.1 対象

対象は asof 当日に価格があり、同日の master に含まれるが、必要な入力履歴を欠いて universe 評価へ入らなかった panel row とする。再構築時に row へこの状態を明示し、diagnostics の件数と対象 row 数が一致しない cache は評価しない。

対象 row は現行 method では valuation metrics、`recommended_rank`、`er_reversion_annual` を持たない。このため後から rank group や E[r] quintile へ所属させない。報告値では実際に観測した forward `price_return` を cohort の母集団中央値へ含め、感度計算だけで対象 row の return を次の値へ置き換える。

| case | 対象 row の `price_return` |
| --- | --- |
| `as_reported` | store にある実現値 |
| `total_loss` | `-1.0` |
| `neutral` | 対象 row を含む置換前の resolved 流動性母集団の中央値 |

置換は対象 row だけに適用し、他の resolved row の return、rank、metric は変えない。対象 row に resolved return が無い、対象集合を同定できない、diagnostics 件数と一致しない場合は安定とみなさず block する。

### 1.2 結論の向き

production authority が必須とする次の 3 metric を比較する。

| metric | 比較量 |
| --- | --- |
| `recommended_rank_top5` | group の `median_excess` |
| `recommended_rank_top10` | group の `median_excess` |
| `er_calibration` | 最上位 quintileの `median_realized_price_excess` − 最下位 quintileの同値 |

既存の delisting 感度判定と同じく、比較量が `> 0` か `<= 0` かを「向き」とする。3 case のいずれかだけが算出不能になる場合も不安定である。3 case とも算出不能の metric は、その欠損が結論を作っていないため、この感度判定では不安定に数えない。ただし metric 自体の reportability は既存の `metric_statuses` が別に判定する。

対象 row が 0 件なら安定とする。1 件以上なら、3 metric すべてについて `as_reported` / `total_loss` / `neutral` の向きが一致するときだけ `direction_stable: true` とする。3y/5y の件数 blocker はこの verdict に置き換え、割れた cohort は `priced_master_without_universe_flips_direction` で block する。

### 1.3 採否基準と報告量

次をすべて満たした場合に契約変更を採用する。

- 合成データで、両代入の向きが割れる cohort が block される。
- 対象外の resolved row が置換前後で変わらない。
- 対象 row を同定できない旧 cache や件数不一致を eligible と誤認しない。
- 3y/5y の満期済み cohort について、変更前後の blocker 件数、変更後の eligible 数、新規 eligible cohort 一覧、向きが割れた cohort 一覧を dated report に固定する。

eligible の増加数は採用の最低条件にしない。増えない場合も、規則が正しく問いへ答えていれば non-adoption ではなく「容量効果なし」と記録する。`entry_price_gap` は原因が異なるため変更せず、内訳だけを報告する。

### 1.4 この結果が意味しないこと

この判定は欠けた valuation metrics や rank を復元せず、母集団から未評価銘柄をなくすものでもない。実現 return の存在を使って、未評価 row が cohort の相対 return 基準を通じて production 結論の向きを作ったかだけを判定する。metric の大きさ、統計的有意性、独立な track record は主張しない。

## 2. WS-2: pre-2019 self-range 変種 panel

### 2.1 固定する変種

production panel の self-range は 750 sessions、bar 入力は 1,200 暦日である。変種は次に固定し、grid search はしない。

| 項目 | 値 |
| --- | --- |
| variant id | `pre2019_self_range_375` |
| self-range | asof 以前の直近 375 sessions |
| bar 入力 | asof 以前の 600 暦日 |
| store | `data/screening/calibration-pre2019/` |
| row quality | 全 row に `self_range_degraded: true` |
| authority | diagnostic only |

600 暦日は 375 sessions の約 1.6 倍で、production の 1,200 / 750 と同じ比率を固定した値である。bar store の床が 2016-08-01 なので、最初の非 clamp 月末は概ね 2018-03、production store が始まる前月 2019-10 まで約 20 cohort になる見込みである。実際の月末 calendar による件数は構築後に報告する。

変種は production store と物理 directory を分け、variant id と窓を含めた `rules_hash` を使う。通常 panel と同じ hash、同じ directory、または `production_decision` での評価を拒否する。production build の既定値と挙動は変えない。

### 2.2 診断対象と評価量

非 clamp で成立した最初の cohort から 2019-10 までを対象とし、1y / 3y の `er_calibration` を使う。各 cohort について次を固定する。

- 最上位 quintileと最下位 quintileの predicted reversion excess 差。
- 同じ 2 群の realized price excess 差。
- `realized / predicted`。predicted が 0 の場合は算出不能とする。
- 母集団数、各 quintile の n、`priced_master_without_universe` を含む coverage flags。

2018 年の correction 診断は 2018-10、2018-11、2018-12 の月末 cohort を事前に固定して抜き出す。比較として、それ以外の pre-2019 cohort の中央値と、既存 production panel の COVID stress 2 cohort（2020-03、2020-04）の既報値を並べる。既報値と軸・horizon が一致しない場合は無理に比較せず、その不一致を報告する。

### 2.3 成立条件と判定

次を満たせば診断 store と report を採用する。

- production store と directory / `rules_hash` の両方が異なる。
- 全 row が `self_range_degraded: true` で、production row は false である。
- diagnostic では読めるが `production_decision` は機械的に拒否される。
- 2018-10〜12 のうち少なくとも 1 cohort が非 clamp で、1y または 3y の `er_calibration` を算出できる。

成立しない場合は store surface を恒久化せず、失敗理由と得られた coverage だけを report に残す。成立しても production の E[r] parameter、screening rules、authority 条件は変更しない。

### 2.4 この結果が意味しないこと

375-session self-range は production input と異なるため、3y/5y production evidence ではない。2018 correction は COVID より浅く短く、追加される観測も forward window が重なるため独立標本ではない。結果は「深い stress 一般」を識別せず、2018 と COVID の差を因果効果としても解釈しない。2016 年は input history の開始年であり、2016 年 cohort が成立するという意味ではない。
