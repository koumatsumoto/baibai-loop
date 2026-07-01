---
title: "Portfolio management"
summary: "資本・許容リスク・ポジション管理・積立と余力・concentration cap・kill switch の正本。macro / screening の上流にある自己運用の governance。"
doc_type: governance
status: active
last_reviewed: 2026-07-01
---

# Portfolio management — 資本とポジションの運用方針

判断ループの上流にある自己運用の governance artifact。目的・制約・資本・許容リスク・ポジション管理・eligible universe・kill switch を明文化し、research / trade が判断時点の前提を再現できるようにする。思想・大戦略は [`doctrine.md`](./doctrine.md)、構造は [`architecture.md`](./architecture.md) を参照。

Baibai-Loop は投資助言サービスではない。これは自分の裁量判断を後から検証するための統制文書である。

## 責務 / 対象外

**責務**：資本と許容リスクの定義、ポジション管理（銘柄数・concentration cap・sizing）、積立と余力の運用、eligible universe、kill switch、最低 payoff / risk-reward 下限の方針、割安で買い割高で全売りする売買規律。

**対象外**：個別銘柄 thesis の説明・screening playbook の条件・entry / target / invalidation の個別設計・実注文の約定記録（これらは [`workflow/`](./workflow/) の research / screening / position が扱う）。playbook（再現可能な thesis pattern・evidence checklist）は [`workflow/playbooks.md`](./workflow/playbooks.md)、最低 payoff の具体判定は [`workflow/research.md`](./workflow/research.md) が扱う。

## 資本モデル

- **単一プール**：投資可能な実資金全体を 1 つの `real_capital_yen` として扱う（二層構造や仮想資本は用いない）。concentration cap の分母はこの `real_capital_yen`（積立に応じて手動で月次更新する簿価）に統一する。
- **初期資産**：既存建玉を含めて ¥10,000,000。
- **月次積立**：毎月 **+¥40 万**が予算に加わる。うち **実投下は月 ¥20–30 万**を基本にし、差額（¥10–20 万 / 月）は **暴落時の押し目買い用の余力**として残す。`real_capital_yen` は積立に応じて月次で更新し、% ベースの cap を自動でスケールさせる。
- **投下タイミングは macro が決める**（[`workflow/macro.md`](./workflow/macro.md)）。高値圏・リスクオフでは余力を厚く保ち、暴落・過度な悲観では余力を投下する。平時は月次の割安候補へ淡々と積み立てる。
- **余力と目標充足**：dry powder は暴落時の押し目買いを主目的とする reserve だが、暴落が来ない局面でも、割安候補があれば平時の月次積立（¥20–30 万）で 15–25 銘柄へ段階的に充足させる。no-crash が長期化しても現金がリスク調整後に支配し続けないよう、余力を無制限には累積させず平時投下を継続する。

## ポジション管理

- **銘柄数**：中庸 **15–25 銘柄**。深い個別調査が回る規模と、個別リスクを薄める分散を両立させる。
- **concentration cap**：単一銘柄 **4–6%**、単一 `sector_33` **30–40%**、ADV 参加率 **5%**。**cap は entry 時の sizing 制約**であり、`real_capital_yen`（簿価）に対する % で評価する。購入後の値上がりで保有時価が cap を超えても縮めない（部分トリムをしないため。cap は維持不変量ではない）。具体閾値は code-managed policy（`src/baibai_loop/position/policy.py`）を正本にし、本 doc は方針を説明する。
- **sizing**：候補の payoff・macro 姿勢・塩漬け耐性・liquidity・policy cap の順で決め、board lot と guard price で丸める。cap 超過分は board lot 単位で減額する。

## 割安 / 割高と売買規律

- **買い**：機械 valuation ranking の **割安ゾーン**（業種相対・自己 5 年レンジ相対の percentile、[`reference/valuation-metrics.md`](./reference/valuation-metrics.md)）と、thesis の **個別フェアバリュー（FV）に対する下方乖離**の両方を満たす銘柄を、長期で積み立てる。
- **売り**：**(a) 割高化（FV 到達・割高ゾーン）** または **(b) 事業の fundamental 毀損**の 2 つだけをトリガーに **全売り**する。部分トリム / リバランスはしない。
- **価格 stop は撤廃する**。株価の逆行では売らない。想定通りに割高化せず含み損が続いても、塩漬けを許容して事業の回復を待つ。その間の資産ロックは受け入れる。
- 配当利回りは加点材料。資産ロック中に収益が見込める配当・自己株買い・安定した shareholder return がある銘柄を優先する。

## 塩漬け耐性 principle（selection の必須ゲート）

価格 stop を撤廃して塩漬けを許容できるのは、**最初から塩漬けに耐える銘柄だけを選ぶ**からである。採用候補は次を必須ゲートで確認する：net-cash または健全な balance sheet、営業 CF が黒字、低い有利子負債と借換リスクの低さ、耐久的な収益基盤。これらを満たさない割安は、どれほど安くても採用しない。

これは損失を無視するための方針ではない。長期保有を続けるのは、事業継続性・cash flow・balance sheet・thesis の中核が維持されている限りである。中核が毀損した場合は上記「売り (b)」で全売りする。

## Kill switch

Kill switch は **事業継続前提の毀損検知と、binary event 直前の建玉タイミング配慮**を指す。

- **fundamental 毀損**（減益トレンド・財務悪化・減配・thesis 中核崩壊・事業継続前提の毀損）：`kill_switch_check` は保有中の継続監視として record 化し、検知したら「売り (b)」で全売りする。これが kill switch の核。
- **binary event 直前の新規建玉**（決算発表跨ぎ・日銀会合前日・FOMC 前日）：新規 entry のタイミング配慮として扱う。長期積立では必須の hard block ではないが、binary event の直前に大きく建てるのは避け、待つか小さく建てる。position record の `kill_switch_check` に事実として残す。

## AI 長期影響と sizing

AI を中心セクターに据える思想は [`doctrine.md`](./doctrine.md) 柱 2 を正本とする。ポジション管理の観点では、AI が長期追い風になり得る銘柄は保有継続の期待値（回収余地）を高め、AI による既存事業の disruption・顧客投資循環の鈍化・valuation 過熱・競争優位の毀損は塩漬け耐性を弱める要因として扱う。

**AI 期待は単独の採用根拠・sizing 根拠にはしない**。採用は valuation・cash flow・balance sheet・catalyst・競争優位・資本配分・決算鮮度と合わせて判断する。

## Eligible universe

- **日本の上場普通株のみ**（ETF / 投信 / 海外株は扱わない）。long-only・現物（信用・レバレッジを使わない）。
- 流動性フィルタ（時価総額・平均売買代金・上場期間・JPX 規制）は分析層のパラメータとして selection 時に適用する（[`workflow/screening.md`](./workflow/screening.md)）。
- **口座・税制（NISA / 特定口座）はモデル化しない**。売買判断は純粋に valuation で行う。

## Policy field との境界

capital・risk・liquidity・concentration の **具体閾値は code-managed policy config（`src/baibai_loop/position/policy.py`）を正本**にし、validator が検査する。本 doc は判断方針と背景を説明する。1 注文サイズは月次予算と % cap で律速し、絶対額の上限は置かない。塩漬け耐性は `durability_gate` field の **記入を schema で必須**にするが、耐性の **合否判定は人間** が行う（機械的 hard gate ではない）。AI 長期影響は strategic attention principle であり、research / macro の template・手順・self-review で担保する。

## 参考

- [`doctrine.md`](./doctrine.md)：思想・大戦略・5 柱
- [`workflow/research.md`](./workflow/research.md)：個別 thesis での FV・RR・期待利回り・耐性の検証
- [`workflow/position.md`](./workflow/position.md)：執行・保有・全売り・見積り calibration
- [`reference/valuation-metrics.md`](./reference/valuation-metrics.md)：割安判定に使う valuation 指標
