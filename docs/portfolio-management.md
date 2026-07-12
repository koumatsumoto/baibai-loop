---
title: "Portfolio management"
summary: "資本・許容リスク・ポジション管理・積立と余力・concentration warningの正本。macro / screening の上流にある自己運用の governance。"
doc_type: governance
status: active
last_reviewed: 2026-07-11
---

# Portfolio management — 資本とポジションの運用方針

判断ループの上流に置く、自己運用の統制文書。目的・制約・資本・許容リスク・ポジション管理・投資対象の範囲を明文化し、research / trade が判断時点の前提を後から再現できるようにする。思想・大戦略は [`doctrine.md`](./doctrine.md)、構造は [`architecture.md`](./architecture.md) を参照。

Baibai-Loop は投資助言サービスではない。これは自分の裁量判断を後から検証するための統制文書である。

## 責務 / 対象外

**責務**：資本と許容リスクの定義、ポジション管理（銘柄数・集中度上限（concentration cap）・投入額の決め方）、積立と余力の運用、投資対象の範囲、thesis health、最低リスクリワードの方針、割安で買い保有を見直す売買規律。

**対象外**：個別銘柄の thesis の説明、screening 条件、entry / 目標価格 / 毀損条件の個別設計、実注文の約定記録（これらは [`workflow/`](./workflow/) の research / screening / position が扱う）。screening の型別条件は `records/_config/screening-rules/*.yaml`、型別の research checklist は [`workflow/playbooks.md`](./workflow/playbooks.md)、最低リスクリワードの具体判定は [`workflow/research.md`](./workflow/research.md) が扱う。

## 資本モデル

資本はcanonical ledgerのevent replayで管理する。

- **repo 内単一 ledger**：対象はこの repository で管理する日本株portfolioだけとし、repo外の保有、海外株、index商品は合算しない。資本額をpolicyや各positionへ手入力しない。
- **event replay**：`opening_balance / contribution / reservation / release / execution / income / cost / tax_confirmed` を時系列に再生し、available cash、未約定引当、取得原価、保有時価、確認済み収益・費用・税を1円単位で再計算する。契約と式は [`reference/portfolio-ledger.md`](./reference/portfolio-ledger.md) を正本とする。
- **月次積立**：毎月 **+¥40万**を `contribution` として記録する。割安候補がなければ投資を強制せず、未使用分はavailable cashとして累積する。好機では通常配分を超えられるが、実在するcashを超えるreservationはhard errorにする。
- **待機資金**：dry powderは恒常的に残す判断目安であり、固定配分のhard gateではない。policyのwarning lineを下回る提案は理由と期限を持つhuman overrideを要求する。
- **将来税**：配当・売却で実際に確認した税だけを`tax_confirmed`へ記録する。将来売却税は任意の実効税率estimateとして別表示し、率が未入力なら`unknown`とする。

## ポジション管理

- **銘柄数**：**15–25 銘柄**を目安にする。深い個別調査が回る規模と、個別銘柄のリスクを薄める分散を両立させる。
- **集中度warning**：単一銘柄 **6%**、単一sector **40%**、共通要因 **35%**をwarning lineとする。分子は保有時価と未約定reservationを合算し、分母はledgerが再計算した`total_capital_yen`を使う。超過は情報を隠さず表示し、最大31日の理由付きhuman overrideを許す。
- **投入額（sizing）**：期待総合リターン、恒久損失リスク、流動性、既存+予約済みexposure、available cashを比較し、単元株数と上限価格で丸める。現金不足だけは提案を成立させない。

## 割安 / 割高と売買規律

- **買い**：機械的な valuation ranking の **割安ゾーン**（業種相対・自己レンジ（約 3 年）相対の percentile、[`reference/valuation-metrics.md`](./reference/valuation-metrics.md)）にあり、かつ thesis の **個別フェアバリュー（FV）より現値が十分に安い**銘柄を、長期で積み立てる。
- **売り**：判断は thesis health と税引後の代替期待値で行い、`hold / add / reduce / exit` を提案する（正本は [`reference/holding-review.md`](./reference/holding-review.md)）。**thesis break（事業のファンダメンタルズ毀損）が優先売却候補**で、全株 exit する。**FV 到達は保有見直しの trigger** であって自動の全売りではない：税・費用を引いた代替機会が現保有を上回るときだけ reduce / exit し、勝る乗換先が無ければ割高でも保有を続けてよい。
- **価格による損切りは置かない**。株価が下がったこと自体では売らない。想定どおりに割高化せず含み損が続いても、塩漬けを許容して事業の回復を待つ。その間、資金が拘束されることは受け入れる。
- 配当利回りは加点材料。資金が拘束されている間も収益が見込める、配当・自社株買い・安定した株主還元のある銘柄を優先する。

## 塩漬け耐性の原則（選定の必須ゲート）

価格による損切りを置かずに塩漬けを許容できるのは、**最初から塩漬けに耐えられる銘柄だけを選ぶ**からである。採用候補には次の確認を必須の関門として課す：ネットキャッシュまたは健全なバランスシート、営業キャッシュフローの黒字、低い有利子負債と借換リスクの低さ、耐久性のある収益基盤。これらを満たさない割安は、どれほど安くても採用しない。

これは損失を無視するための方針ではない。長期保有を続けるのは、事業の継続性・キャッシュフロー・バランスシート・thesis の中核が保たれている限りである。中核が毀損（thesis break）した場合は全株 exit する。

## 保有見直しと売却規律

保有中の売却判断は **thesis health**（invalidation・永久損失 7 軸・証拠鮮度・現値起点の 5 年期待値）と、**税引後の代替機会費用** で `hold / add / reduce / exit` を提案する。契約と算術の正本は [`reference/holding-review.md`](./reference/holding-review.md)。

- **thesis break が優先売却候補**：invalidation の発火、または verified な adverse 永久損失軸（減益トレンド・財務悪化・減配・希薄化・顧客集中・構造衰退・経営会計警告）を検知したら全株 exit する。
- **FV 到達は review trigger**：割高化しても自動売却しない。税・費用を引いた代替候補が現保有を上回るときだけ reduce / exit する。税額が確定できない（NISA・損益通算）場合は単一の結論を断定せず、感応度（乗換が有利になる閾値税率）を示す。
- **binary event 直前の新規建玉**（決算発表跨ぎ・日銀会合前日・FOMC 前日）：新規 entry のタイミングへの配慮として扱う。長期の積立では必須の禁止事項ではないが、こうしたイベントの直前に大きく建てることは避け、待つか小さく建てる。

## AI の長期影響

AIは中心sectorではなく、企業別のvalue-capture判断として扱う。AIによる需要増が、競争優位・価格決定力・必要capex・顧客交渉力を通じて株主価値へ変換されるかをdecision packetで確認する。既存事業のdisruption、顧客の投資サイクル鈍化、valuation過熱、競争優位の毀損は永久損失リスクとして扱う。

AIへの期待は、それ単独では採用理由にも投入額の根拠にもしない。投入額は期待総合リターン、永久損失リスク、流動性、既存exposure、available cashで決める。

## 投資対象の範囲（eligible universe）

- **日本の上場普通株のみ**（ETF / 投資信託 / 海外株は扱わない）。買い建てのみ・現物のみ（信用取引・レバレッジは使わない）。
- 流動性の絞り込み（時価総額・平均売買代金・上場期間・JPX 規制）は分析層のパラメータとして selection 時に適用する（[`workflow/screening.md`](./workflow/screening.md)）。
- **口座種別（NISA / 特定口座）はモデル化しない**。確認済み税額と、売却比較に必要な任意の実効税率estimateだけを扱う。

## Policy field との境界

cash・保有・未約定引当の正本はportfolio ledger、warning lineと単元制約の正本は`src/baibai_loop/position/policy.py`とする。各artifactの手計算した集中度を資本の正本にしない。validatorはledgerのreconciliation errorをhard error、concentration・dry powder超過をwarningとして報告する。

## 参考

- [`doctrine.md`](./doctrine.md)：思想・大戦略・5 つの柱
- [`workflow/research.md`](./workflow/research.md)：個別 thesis での FV・RR・期待利回り・耐性の検証
- [`workflow/position.md`](./workflow/position.md)：執行・保有・全売り・portfolio outcome
- [`reference/valuation-metrics.md`](./reference/valuation-metrics.md)：割安判定に使う valuation 指標
