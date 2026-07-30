---
title: "廃止銘柄の除外を有界バイアスとして扱う判定の事前登録"
summary: "exit value を持たない廃止銘柄が cohort を production evidence から外す現行契約を、結論の向きが両側代入で不変なら通す判定へ改める。基準は計測前に固定する。"
doc_type: measurement-record
status: active
last_reviewed: 2026-07-31
---

# 廃止銘柄の除外を有界バイアスとして扱う判定 — 事前登録

## 0. 事前登録（拡張 grid の評価を実行する前に commit する）

現行契約は `unpriced_exit_count` が 1 件でもあれば cohort を production evidence から外す。これを次の判定へ置き換えることを、**拡張 grid の eligibility を評価する前に**ここで固定する。

### 判定規則

cohort の metric について、exit value を持たない銘柄へ次の 2 通りを代入して metric を再計算する。

| 代入 | 値 | 意味 |
| --- | --- | --- |
| 全損 | `price_return = -1.0` | その銘柄が無価値になったとみなす下限 |
| 中立 | 同 cohort の resolved 銘柄の中央値 | 廃止が結果に情報を持たないとみなす基準 |

**両方の代入で結論の向きが一致するときだけ eligible とする。** 向きが割れる cohort は `unpriced_exit` blocker を維持する。

「結論の向き」は metric ごとに次で定める。

| metric | 向き |
| --- | --- |
| `recommended_rank_top5` / `recommended_rank_top10` | 推奨上位の median excess return の符号 |
| `er_calibration` | 予測 E[r] と実現リターンの rank IC の符号 |

### 採否基準

- 両側代入の実装が、代入前後で resolved 銘柄の値を変えないこと（代入は unresolved 行にだけ効く）。
- 拡張 grid の 3y / 5y 満期済み cohort について、両側代入の結果と向きの一致・不一致を全件表に出すこと。
- 向きが割れた cohort を eligible にしないこと。

### この判定を採る理由

除外率に閾値（例「5% 未満なら許す」）を置く形も考えたが採らない。閾値は恣意的で、除外が結論を反転させ得るかどうかを直接には答えない。両側代入は「除外された銘柄がどんな値であっても結論は変わらない」を cohort ごとに示すので、答えるべき問いに直接答える。

### 誠実性の限定（先に開示する）

- **盲検ではない。** `reports/2026-07-30-calibration-evidence-eligibility.md` §5.1 で、断面 master 適用後の 2022-09-30 cohort に `unpriced_exit` が 239 件 / 3,771 出ることを既に公表している。規則の設計者はこの水準を知った状態で規則を書いた。
- **上側の bracket は保守的でない。** TOB / MBO による廃止はプレミアム付きで、実現リターンは中立代入より上に出る。2 通りの代入はその場合の真値を挟まない。したがってこの判定は「除外が結論を**下**へ引く可能性」に対しては bracket になるが、上へ引く可能性に対しては中立代入が下限になるだけである。廃止の内訳（上場廃止 / 買収 / 経営統合）を件数で出し、買収由来が多い cohort ではこの限定を明記する。
- **この判定は exit value を作らない。** 廃止時の実際の対価を外部 source から取る道（#420）は依然として別に存在し、両側代入はそれが無い状態で結論の頑健性だけを述べる。

## 1. 対象

拡張 grid（`backfill-history` による 10 年 backfill 後）の 3y / 5y 満期済み cohort。

## 2. 計測

（拡張 grid の再構築と評価の後に記入する）

## 3. 判定

（同上）
