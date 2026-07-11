---
name: macro-analysis
description: >-
  マクロ経済分析（環境読み・相場 regime・地合い/リスクオンオフ・金融環境・FOMC/利上げ見通し・
  ドル円/クレジットスプレッド・BTC等の中長期見通し）を行うときの操作手順と公開前の品質ゲート。
  material deltaに必要な指標・一次情報の取得 → 敵対的 self-check → records/reports への落とし込みまでを、
  「自分が引いたデータと自分の結論が矛盾しない」検算規律つきで回す。「マクロ環境を分析」「相場局面/
  regime を読む」「macro-context を書く」「金利/流動性/為替のマクロ見通し」等のときに使う。
---

# マクロ経済分析の方法（Baibai-Loop）

このスキルは **マクロ環境分析の「操作」と「公開前の品質ゲート」** を担う。思想・provider 設計・**汎用レンズの読み方（8 レンズ）**・誠実性ファイアウォールの正本は [`docs/workflow/macro.md`](../../../docs/workflow/macro.md)。trigger選択とe2e導線は [`docs/operations/decision-cycle.md`](../../../docs/operations/decision-cycle.md)。formal な calibration/retro ループは持たない（正本冒頭「形式化した独自ループにはしない」と同じ精神）— §5 は「手順を都度洗練する」だけ。

> **鉄則 — 自分のデータと結論を照合する**
> 結論を書く前に、自分が引いた series の実値と結論が矛盾していないかを必ず照合する。方向を語る前に range を引き、結論を**反証する** series の実値が反証側に振れていないか確認する（§3-1）。
> 例: M2 が前年比で加速し FRB 総資産も再拡大している局面で「流動性にエンジンが無い」と書けば、引いた series の実値と逆を向く。

マクロは N≈1 の判断。本スキルは edge 数値・統計的有意・自動 sizing を出さない。得るのは再現性と grounding。

## 更新トリガー（いつ環境読みを更新するか）

macro-context は **定期生成しない**（cron 化しない）。次のトリガーで「必要時に」更新する（workflow/macro.md ②「更新のきっかけ」と整合）:
- **material change後**: discount rate、需要、資金調達、共通tail riskのいずれかが個別5年期待値を変えると判断できるときだけ更新する。selectはcontext不在/staleをwarningとして継続し、future contextだけをERRORにする。
- **主要イベント後**: FOMC / BOJ / ECB / 米 CPI・PCE・NFP / 地政学ショックを、material deltaの有無を確認する契機にする。変化がなければrecordは作らない。
- **前回 `refresh_triggers` の発火**: 前回 context が「前提が崩れる条件」とした事象が起きたとき。

固定 cadence の網羅蓄積を目的化しない。トリガーが無ければ作らない。

## 1. 手順（end-to-end）

1. **変化channelを特定する** — discount rate、需要、資金調達、共通tail riskのどれが個別5年期待値を変え得るかを先に置く。該当しなければ終了する。
2. **必要なsourceだけを引く** — channelを確認できる基盤seriesと一次情報を取り、方向を語るseriesだけはrangeも引く（`--latest`単点で方向を断じない）。
3. **個別判断への経路を言語化する** — researchで確認すべき因果と反証を短く残す。全レンズの結論やsector姿勢を作らない。
4. **該当する§3ゲートを通す** — 1つでも✗ならその結論を書かない。
5. **落とし込む** — material deltaがある場合だけrecordsへ、特殊調査はreportsへ置き、編集後にvalidationを実行する。

KAIZEN は §3 末尾の掃き出しチェックで回す（§5）。

## 2. 必要なsourceの取得（操作）

`baibai-loop-macro get` は 1 series ずつ。出力カラムは `series_id  observed_at  value  unit  provider  source`（`source`=cache/provider）。束ねて latest を引く（決定論・cache miss 時だけ provider）。

```bash
cd /home/kou/baibai-loop
# registry が登録 series の正本。固定 series リストを skill に持たず list から束ねる（series 追加で skill が陳腐化しない）
uv run baibai-loop-macro list                     # 全 series を id/name/category/geo/freq/unit/provider で一覧
uv run baibai-loop-macro list --category energy   # レンズ別に絞る例（rates/credit/fx/energy/liquidity 等）
# 下の表はchannel別の候補であり、毎回全series・全レンズを取得するチェックリストではない。今回のmaterial deltaを確認するものだけを逐次に引く。
# list の 1 列目が series_id。category ごとに id を取り、1 series ずつ get する（並行起動しない・§2 末尾の注意）:
for s in $(uv run baibai-loop-macro list --category rates | cut -f1); do
  uv run baibai-loop-macro get "$s" --latest 2>&1 | tail -1
done
# 注意: get --latest は取得済み窓内では provider を呼ばず cache 最新を返す。
# 環境認識を書く直前は主要 series を refresh --start <直近> --end <today> してから読む。
# 方向が論点の series は range で（窓は §3 の標準窓。日付はそのまま走る）:
uv run baibai-loop-macro get us.m2         --start "$(date -d '14 months ago' +%F)" --end "$(date +%F)"
uv run baibai-loop-macro get us.fed_assets --start 2025-01-01 --end "$(date +%F)"
uv run baibai-loop-macro get btc_usd       --start "$(date -d '3 months ago' +%F)"  --end "$(date +%F)"
```

注意:
- `us.m2` / `us.fed_assets` は **level**。「前年比/加速」を語るなら YoY を自分で計算する（§3-4）。
- **net liquidity = `us.fed_assets` − `us.reverse_repo` − `us.tga`** の 3 成分を揃えて読む（**単位注意: `us.reverse_repo` は十億ドル、`us.fed_assets`/`us.tga` は百万ドル**＝換算して引く）。1 成分でも欠けたまま「net liquidity が増/減」と断定しない（§3-1 の反証漏れを防ぐ）。
- `us.sp500` / `us.nasdaq` / `jp.nikkei225` / `us.2y` / `usd_jpy` は **context anchor**（リスク資産・カーブ・キャリーの背景）で、下の 4 レンズの直接入力ではない。
- `us.fed_assets` 等の大きな値は scientific notation で出る（`6.73564e+06` = 6,735,640 百万ドル = $6.74T）。桁を取り違えない。
- `get` を**並行起動しない**（複数プロセスを同時実行すると stdout / cache が混線し、無関係な series 値が混入する）。上のループで逐次に引く。`yahoo`/`multpl` の cache miss が混ざるときは各呼び出しを `timeout` でラップする。

| レンズ | 束ねて引く series |
| --- | --- |
| グローバル流動性 | `us.fed_assets`・`us.reverse_repo`・`us.tga`・`us.m2` |
| 実質金利・store-of-value | `us.real_10y`・`us.breakeven_10y`・`usd_index.broad`・`gold`・`silver`（金銀レシオ） |
| 金融環境の合成 | `us.nfci`・`vix`・`us.move`・`credit.us_*_oas` |
| リスク選好の温度計 | `btc_usd`・`vix`・`credit.us_hy_oas`/`credit.us_ccc_oas`・`us.nfci` |
| 景気サイクル・breadth | `us.initial_claims`・`us.industrial_production`・`us.gdp_growth`・`copper`・`us.russell2000`・`us.10y_3m_spread`・`us.sox` |
| バリュエーション・ERP | `us.sp500_earnings_yield`・`us.sp500_cape`・`us.sp500_pe`・`us.10y`（ERP=益回り−名目10y） |
| グローバル中銀の同期 | `us.fed_funds.upper`・`jp.policy_rate`・`ecb.policy_rate` |
| エネルギー・地政学 | `wti`・`brent`（原油の戦争プレミアム）・`gold`（有事の安全資産）。供給ショック時は `usd_jpy` と併読 |

**各レンズが何を意味するか（読み方）は [`docs/workflow/macro.md`](../../../docs/workflow/macro.md) ③「8 分析レンズ」が正本。** ここでは「どの ID を束ねて引くか」だけ示す。

## 3. 公開前の self-check ゲート（1 つでも✗なら結論を書かない）

1. **データ⇄結論の整合（反証テーブルで残す・最重要）**: 各方向コール/結論について、本文か sidecar に 3 列を書く — (a) 結論, (b) 支持する series 実値＋日付＋引いた range, (c) **この結論を反証するならどの series のどの値か／その実値は反証側に振れていないか**。(c) が空 or 実値が反証側を向く結論は書かない。支配的ドライバー（BTC なら流動性）は必ず (c) を埋める。
2. **トレンドの窓を先に決める（窓 cherry-pick 禁止）**: 方向（加速/減速/横ばい/拡大/枯渇）を語る series は、結論を見る前に信号の自然周期で range を引く — YoY 系 ≥13 か月／QT・QE は QT 開始以降の全区間／BTC・リスク選好 ≥3 か月／金利水準 ≥6 か月。`--latest` 単点・数日 range で方向を断じない。「加速」は変化率自体が上向き（2 階差）であることを range で示す。
3. **series の鮮度・段差・廃止**: 方向に使う各 series の最終実測日を確認（`--latest` が今日に近いか）。FRED 廃止系列（金 LBMA 2025/5 停止・JP OECD 2021 停止）・release lag・rebase/methodology 変更で、stale な最終値や段差を「横ばい/異常」と誤読していないか。**FRED 商品系（`wti`/`brent`=DCOIL系）は数日ラグがあり、地政学急変時は直近の spike/relief を取りこぼす — `refresh` か `yahoo` 先物・EIA STEO と突き合わせ最終実測日を明記する。** cache hit の決定論は鮮度を保証しない。[AP-03]
4. **単位・系列種別・基準**: 各数値に種別(level/MoM/YoY/年率/SA・NSA)・単位(%/bp/pt/倍/通貨)・方向コールの基準(長期平均/直近3か月/0ライン)を付したか。`us.m2` は level なので「前年比」を使うなら YoY を計算して残したか。`us.nfci` 等の符号(正=引締)を取り違えていないか。**水準コール（高い/低い/タイト/割高/割安）は絶対値でなく実測分布の percentile / z-score で定量化したか**（VIX 18.9 は絶対では低く見えるが 65%ile なら「無警戒」ではない／IG OAS 10%ile と CCC OAS 89%ile の乖離で dispersion を示す）。長期窓を引いて現在値の分位を出す。
5. **比率・差分の検算（計算を本文に残す）**: 出典の比率を転記せず再計算し、本文に `(計算: A/B=C)` を残したか。出典自体が内部不整合でないか（例: $1.2B/$0.477B≈2.5 ≠ 3.5:1）。[AP-02]
6. **provenance の混在**: 基盤 series と外部 web を 1 つの数値（例 ドローダウン%）に混ぜていないか。混ぜるなら各値に source を付し、値の不一致（例 BTC 基盤$60k vs web$63-64k）を注記したか。**パネルに在る series（`usd_jpy`/`vix`/`gold`/株価指数/`wti`/`brent` 等）を WebSearch で取り直さない — 要約由来のズレ（gold 基盤4078 vs web4224 等）が入る。WebSearch は series 化できない出来事（地政学イベント・政策声明）の事実確認に限定し、数値は基盤 series を一次資料にする。一次 URL を直接取得できるときは WebSearch より優先し、日本語 query は unavailable になりやすいため WebSearch が必要なときは英語 query を使う。** [AP-01]
7. **テープ前にベースレート**: 確率を出す前に、直近値動きを見ない無条件ベースレート（長期分布/事前確率）を先に書き、直近テープがそれをどれだけ・なぜ動かしたかを明示したか。過去 context の「分析・結論」を前提にしていないか（独立性は workflow/macro.md ②「分析の独立性」）。
8. **レンズ間矛盾の調停**: 4 レンズが矛盾（流動性=追い風だが金融環境=引締 等）していないか。矛盾を「今どちらが支配的か／slow-burn か」で明示裁定し、総合結論が 1 レンズ依存になっていないか。援用する経験則（M2 ~10週先行・実質金利↑＝金/BTC 逆風）が現レジームで反転/decouple していないか一言添えたか。
9. **直近 release の最新性**: 発行日±5営業日の FOMC/CPI/BOJ/PCE/NFP が出て前提を覆していないか確認したか。[AP-07]
10. **誠実性ファイアウォール**: edge 数値・統計的有意・自動 sizing を出していないか。シナリオ確率は主観と明示し、合計≈1・相互排他・網羅・horizon 一致か。
11. **スコープ分離**: 汎用指標と特殊対象（例 BTC トレジャリーの mNAV・転換社債）を分け、特殊を基盤（`series.yaml`/workflow doc）に入れていないか。
12. **機械検証**: macro-context を編集したら `uv run baibai-loop-validation --target macro-context` を通したか（schema/additionalProperties/series 一致）。[AP-08]
13. **KAIZEN 掃き出し**: この run で踏んだ手順の穴を `./KAIZEN.md` に拾い、再現する手続き的欠陥は本 SKILL の該当節へ畳んで KAIZEN を空にしたか（§5）。

## 4. 落とし込み

- 環境読み → `records/01-macro-context/<YYYY>/<MM>/...yaml`。articles/indicator seriesには横断一意な`input_id`、status、used_forを持たせる。material deltaとsizing cautionはそのinput_idを参照する。screenはmacro-blindのまま。
- 特殊な単発調査（例 暗号資産トレジャリーの財務）→ `reports/<YYYY-MM-DD>-<slug>.md`。基盤に入れない特殊対象はここに閉じる。
- **編集後に `uv run baibai-loop-validation --target macro-context` を通す**（§3-12）。新しい汎用 series が要るなら `series.yaml` に 1 行足し、必ず `--latest` で live 取得を実 fetch 確認（FRED は廃止系列あり。workflow/macro.md ①のデータソース registry）。

## 4.5 任意の人間向けHTMLレポート

通常運用ではYAMLの短いsummaryとmaterial deltaだけを残す。HTMLは別途共有価値がある調査だけで作成し、macro recordとの併産や12セクション構成を要求しない。雛形は必要時の参考実装に留める。

- **規律**: 単一 HTML・インライン CSS・外部依存ゼロ（オフラインで開く）。水準は percentile バッジで定量化（§3-4）、基盤 series と web を出典で分離（§3-6, web は source 明記）。§3 ゲートを全て通してから書く。脆い provider（`multpl`）の数値は注記する。
- **出力先と表示**: `.plan/macro-report-<date>.html`（ドラフト）に書き `open-file` skill で既定ブラウザに表示。確定版は records/reports へ。逐次取得（§2 の並行起動禁止）を厳守する。

## 5. このスキルの自己改善（§3 ゲートを育てる）

正味の仕事は **§3 ゲートを育てること**で、専用儀式を増やさない。§3 を通すついでに回す。

- **拾う**: この run で踏んだ手順の穴・摩擦・不足レンズを 1 行で `./KAIZEN.md` に書く（強制は §3-13 の掃き出し時の 1 回 sweep、即時メモは任意）。
- **畳む基準**: 「別の fresh agent が同手順で同じ穴に落ちる＝再現する手続き的欠陥」なら該当節（多くは §3、必要なら §1/§2/§4）へ恒久化し、本文には「今このチェックが要る理由」を現在形で書く。1 回限りの typo・その日の事情は捨てる。
- **掃き出す**: 畳んだ／捨てた項目は KAIZEN.md から消す。fold は **1 commit**（KAIZEN 削除＋SKILL 追記）で残し、理由は commit message に書く。これで KAIZEN.md は常に「未反映だけ」、git history が判断根拠の安全網になる。
- **置き場**: 手続き的チェック → 本 SKILL §3／データ取得・source 手順の知見 → workflow/macro.md ①／複数サブシステム横断の普遍的失敗（PR review で 2 回以上の型）→ `docs/anti-patterns.md` へ昇格。
- `KAIZEN.md` は本スキル同梱の skill-scoped backlog。repo 全体の `.plan/` scratch とは別に、スキルと一緒に travel し SKILL.md から 1 ホップで辿れるよう同梱・commit する。
- **前提検証（forward calibration ではない）**: 次回更新時に、前回 context の `refresh_triggers` が発火したか（前提が崩れたか）を確認し `changes_since_previous` に記録する。「予測が当たったか」ではなく「前提の鮮度」を追う（誠実性ファイアウォール: マクロは track record を出さない）。発火の早すぎ/遅すぎは `refresh_triggers` 設定の改善に回す。

## 6. 参照
- [`docs/workflow/macro.md`](../../../docs/workflow/macro.md): 思想・provider registry・**汎用レンズの読み方（正本）**・接続・誠実性。
- [`anti-patterns.md`](../../../docs/anti-patterns.md): AP カタログ（一次情報 AP-01・検算 AP-02・異常値 cross-check AP-03・最新性 AP-07・validator AP-08 等）。
- 改善 backlog: [`./KAIZEN.md`](./KAIZEN.md)。
