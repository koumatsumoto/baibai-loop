---
name: macro-analysis
description: >-
  マクロ経済分析（環境読み・相場 regime・地合い/リスクオンオフ・金融環境・FOMC/利上げ見通し・
  ドル円/クレジットスプレッド・BTC等の中長期見通し）を行うときの操作手順と公開前の品質ゲート。
  指標パネルの取得 → 4 レンズでの解釈 → 敵対的 self-check → records/reports への落とし込みまでを、
  「自分が引いたデータと自分の結論が矛盾しない」検算規律つきで回す。「マクロ環境を分析」「相場局面/
  regime を読む」「macro-context を書く」「金利/流動性/為替のマクロ見通し」等のときに使う。
---

# マクロ経済分析の方法（Baibai-Loop）

このスキルは **マクロ環境分析の「操作」と「公開前の品質ゲート」** を担う。思想・provider 設計・**汎用レンズの読み方**・誠実性ファイアウォールの正本は [`macro-runbook.md`](../../../docs/operations/macro-runbook.md)。formal な calibration/retro ループは持たない（runbook 冒頭「formal なループにはしない」と同じ精神）— §5 は「手順を都度洗練する」だけ。

> **鉄則 — 自分のデータと結論を照合する**
> 結論を書く前に、自分が引いた series の実値と結論が矛盾していないかを必ず照合する。方向を語る前に range を引き、結論を**反証する** series の実値が反証側に振れていないか確認する（§3-1）。
> 例: M2 が前年比で加速し FRB 総資産も再拡大している局面で「流動性にエンジンが無い」と書けば、引いた series の実値と逆を向く。

マクロは N≈1 の判断。本スキルは edge 数値・統計的有意・自動 sizing を出さない。得るのは再現性と grounding。

## 1. 手順（end-to-end）

1. **パネルを引く** — §2。方向を語る series は §3 の標準窓で range も引く（`--latest` 単点で方向を断じない）。
2. **主要ドライバーを基盤 series で ground** — 数値＋日付＋`series_id`。基盤/一次を優先、外部 web は provenance を明示。
3. **4 レンズで方向を言語化** — 束ね方は §2、読み方の正本は runbook §③。レンズ間の矛盾は裁定する。
4. **§3 ゲートを通す** — 1 つでも✗なら結論を書かない。
5. **落とし込む** — §4（環境読み=records、特殊調査=reports、編集後に validation 実行）。

KAIZEN は §3 末尾の掃き出しチェックで回す（§5）。

## 2. パネル取得（操作）

`baibai-loop-macro get` は 1 series ずつ。出力カラムは `series_id  observed_at  value  unit  provider  source`（`source`=cache/provider）。束ねて latest を引く（決定論・cache miss 時だけ provider）。

```bash
cd /home/kou/baibai-loop
# latest パネル
for s in \
  us.m2 us.fed_assets us.reverse_repo us.tga \
  us.real_10y us.breakeven_10y us.10y us.2y us.10y_3m_spread \
  us.cpi.core us.pce.core us.inflation_5y5y \
  us.nfci vix us.move credit.us_hy_oas credit.us_ig_oas credit.us_ccc_oas \
  usd_index.broad usd_jpy btc_usd gold copper \
  us.initial_claims us.industrial_production \
  us.sp500 us.nasdaq us.russell2000 us.sox jp.nikkei225 ; do
  uv run baibai-loop-macro get "$s" --latest 2>&1 | tail -1
done
# 方向が論点の series は range で（窓は §3 の標準窓。日付はそのまま走る）
uv run baibai-loop-macro get us.m2         --start "$(date -d '14 months ago' +%F)" --end "$(date +%F)"
uv run baibai-loop-macro get us.fed_assets --start 2025-01-01 --end "$(date +%F)"
uv run baibai-loop-macro get btc_usd       --start "$(date -d '3 months ago' +%F)"  --end "$(date +%F)"
```

注意:
- `us.m2` / `us.fed_assets` は **level**。「前年比/加速」を語るなら YoY を自分で計算する（§3-4）。
- **net liquidity = `us.fed_assets` − `us.reverse_repo` − `us.tga`** の 3 成分を揃えて読む（**単位注意: `us.reverse_repo` は十億ドル、`us.fed_assets`/`us.tga` は百万ドル**＝換算して引く）。1 成分でも欠けたまま「net liquidity が増/減」と断定しない（§3-1 の反証漏れを防ぐ）。
- `us.sp500` / `us.nasdaq` / `jp.nikkei225` / `us.2y` / `usd_jpy` は **context anchor**（リスク資産・カーブ・キャリーの背景）で、下の 4 レンズの直接入力ではない。
- `us.fed_assets` 等の大きな値は scientific notation で出る（`6.73564e+06` = 6,735,640 百万ドル = $6.74T）。桁を取り違えない。

| レンズ | 束ねて引く series |
| --- | --- |
| グローバル流動性 | `us.fed_assets`・`us.reverse_repo`・`us.tga`・`us.m2` |
| 実質金利・store-of-value | `us.real_10y`・`us.breakeven_10y`・`usd_index.broad`・`gold` |
| 金融環境の合成 | `us.nfci`・`vix`・`us.move`・`credit.us_*_oas` |
| リスク選好の温度計 | `btc_usd`・`vix`・`credit.us_hy_oas`/`credit.us_ccc_oas`・`us.nfci` |
| 景気サイクル・breadth | `us.initial_claims`・`us.industrial_production`・`copper`・`us.russell2000`・`us.10y_3m_spread`・`us.sox` |

**各レンズが何を意味するか（読み方）は [runbook §③「汎用分析レンズ」](../../../docs/operations/macro-runbook.md) が正本。** ここでは「どの ID を束ねて引くか」だけ示す。

## 3. 公開前の self-check ゲート（1 つでも✗なら結論を書かない）

1. **データ⇄結論の整合（反証テーブルで残す・最重要）**: 各方向コール/結論について、本文か sidecar に 3 列を書く — (a) 結論, (b) 支持する series 実値＋日付＋引いた range, (c) **この結論を反証するならどの series のどの値か／その実値は反証側に振れていないか**。(c) が空 or 実値が反証側を向く結論は書かない。支配的ドライバー（BTC なら流動性）は必ず (c) を埋める。
2. **トレンドの窓を先に決める（窓 cherry-pick 禁止）**: 方向（加速/減速/横ばい/拡大/枯渇）を語る series は、結論を見る前に信号の自然周期で range を引く — YoY 系 ≥13 か月／QT・QE は QT 開始以降の全区間／BTC・リスク選好 ≥3 か月／金利水準 ≥6 か月。`--latest` 単点・数日 range で方向を断じない。「加速」は変化率自体が上向き（2 階差）であることを range で示す。
3. **series の鮮度・段差・廃止**: 方向に使う各 series の最終実測日を確認（`--latest` が今日に近いか）。FRED 廃止系列（金 LBMA 2025/5 停止・JP OECD 2021 停止）・release lag・rebase/methodology 変更で、stale な最終値や段差を「横ばい/異常」と誤読していないか。cache hit の決定論は鮮度を保証しない。[AP-03]
4. **単位・系列種別・基準**: 各数値に種別(level/MoM/YoY/年率/SA・NSA)・単位(%/bp/pt/倍/通貨)・方向コールの基準(長期平均/直近3か月/0ライン)を付したか。`us.m2` は level なので「前年比」を使うなら YoY を計算して残したか。`us.nfci` 等の符号(正=引締)を取り違えていないか。
5. **比率・差分の検算（計算を本文に残す）**: 出典の比率を転記せず再計算し、本文に `(計算: A/B=C)` を残したか。出典自体が内部不整合でないか（例: $1.2B/$0.477B≈2.5 ≠ 3.5:1）。[AP-02]
6. **provenance の混在**: 基盤 series と外部 web を 1 つの数値（例 ドローダウン%）に混ぜていないか。混ぜるなら各値に source を付し、値の不一致（例 BTC 基盤$60k vs web$63-64k）を注記したか。[AP-01]
7. **テープ前にベースレート**: 確率を出す前に、直近値動きを見ない無条件ベースレート（長期分布/事前確率）を先に書き、直近テープがそれをどれだけ・なぜ動かしたかを明示したか。過去 context の「分析・結論」を前提にしていないか（独立性は runbook ②）。
8. **レンズ間矛盾の調停**: 4 レンズが矛盾（流動性=追い風だが金融環境=引締 等）していないか。矛盾を「今どちらが支配的か／slow-burn か」で明示裁定し、総合結論が 1 レンズ依存になっていないか。援用する経験則（M2 ~10週先行・実質金利↑＝金/BTC 逆風）が現レジームで反転/decouple していないか一言添えたか。
9. **直近 release の最新性**: 発行日±5営業日の FOMC/CPI/BOJ/PCE/NFP が出て前提を覆していないか確認したか。[AP-07]
10. **誠実性ファイアウォール**: edge 数値・統計的有意・自動 sizing を出していないか。シナリオ確率は主観と明示し、合計≈1・相互排他・網羅・horizon 一致か。
11. **スコープ分離**: 汎用指標と特殊対象（例 BTC トレジャリーの mNAV・転換社債）を分け、特殊を基盤（`series.yaml`/runbook）に入れていないか。
12. **機械検証**: macro-context を編集したら `uv run baibai-loop-validation --target macro-context` を通したか（schema/additionalProperties/series 一致）。[AP-08]
13. **KAIZEN 掃き出し**: この run で踏んだ手順の穴を `./KAIZEN.md` に拾い、再現する手続き的欠陥は本 SKILL の該当節へ畳んで KAIZEN を空にしたか（§5）。

## 4. 落とし込み

- 環境読み → `records/01-macro-context/<YYYY>/<MM>/...yaml`。`inputs.indicator_series[]` に使った series を `series_id`＋`window`＋`used_for` で grounding（series_id は registry 一致必須、未登録は validator が warning）。読みは `sector_tilts`・`research_questions`・`refresh_triggers` へ。screen は macro-blind のまま。
- 特殊な単発調査（例 暗号資産トレジャリーの財務）→ `reports/<YYYY-MM-DD>-<slug>.md`。基盤に入れない特殊対象はここに閉じる。
- **編集後に `uv run baibai-loop-validation --target macro-context` を通す**（§3-12）。新しい汎用 series が要るなら `series.yaml` に 1 行足し、必ず `--latest` で live 取得を実 fetch 確認（FRED は廃止系列あり。runbook ①）。

## 5. このスキルの自己改善（§3 ゲートを育てる）

正味の仕事は **§3 ゲートを育てること**で、専用儀式を増やさない。§3 を通すついでに回す。

- **拾う**: この run で踏んだ手順の穴・摩擦・不足レンズを 1 行で `./KAIZEN.md` に書く（強制は §3-13 の掃き出し時の 1 回 sweep、即時メモは任意）。
- **畳む基準**: 「別の fresh agent が同手順で同じ穴に落ちる＝再現する手続き的欠陥」なら該当節（多くは §3、必要なら §1/§2/§4）へ恒久化し、本文には「今このチェックが要る理由」を現在形で書く。1 回限りの typo・その日の事情は捨てる。
- **掃き出す**: 畳んだ／捨てた項目は KAIZEN.md から消す。fold は **1 commit**（KAIZEN 削除＋SKILL 追記）で残し、理由は commit message に書く。これで KAIZEN.md は常に「未反映だけ」、git history が判断根拠の安全網になる。
- **置き場**: 手続き的チェック → 本 SKILL §3／データ取得・source 手順の知見 → runbook §③／複数サブシステム横断の普遍的失敗（PR review で 2 回以上の型）→ `docs/anti-patterns.md` へ昇格。
- `KAIZEN.md` は本スキル同梱の skill-scoped backlog。repo 全体の `.plan/` scratch とは別に、スキルと一緒に travel し SKILL.md から 1 ホップで辿れるよう同梱・commit する。

## 6. 参照
- [`macro-runbook.md`](../../../docs/operations/macro-runbook.md): 思想・provider registry・**汎用レンズの読み方（正本）**・接続・誠実性。
- [`anti-patterns.md`](../../../docs/anti-patterns.md): AP カタログ（一次情報 AP-01・検算 AP-02・異常値 cross-check AP-03・最新性 AP-07・validator AP-08 等）。
- 改善 backlog: [`./KAIZEN.md`](./KAIZEN.md)。
