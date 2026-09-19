---
title: "Position Review reference"
summary: "対象保有の企業評価と残存見返りからhold / exit / nullを決める契約。"
doc_type: reference
status: active
related_docs:
  - "../doctrine.md"
  - "../portfolio-management.md"
  - "./portfolio-ledger.md"
  - "./thesis.md"
---

# Position Review

<a id="purpose-and-activation"></a>

## 目的と境界

保有1件の継続または現金回収を判断する。型とpure ruleは`research/position_review.py`、build/check/publishは`research/position_review_service.py`。企業別評価はReviewed Thesisを共用し、7軸・valuation・sourceをmirrorへ複写しない。人間が最終判断・broker操作を所有する。

<a id="inputs"></a>

## 入力

対象holding、quote、Reviewed Thesisと、作者が評価した残存見返りを使う。残存見返りはsufficient・insufficient・uncertainと経済的理由で示し、未評価ならnullとする。企業評価全文、他候補、追加購入contextを複写しない。厳密なfieldとversionはmodelを参照する。

<a id="decision-table"></a>

## Action

| 条件 | 提案 |
| --- | --- |
| 重要な一次根拠でinvestment caseの重大なbreakを確認 | exit候補。残存評価・quote不明でも許す |
| case intact、同日企業評価と現在quote・権利単位が有効、残存見返りsufficient | hold |
| 同条件で残存見返りinsufficient | exit |
| 根拠・成立性・残存見返りがuncertainまたは未評価 | null。理由と人間確認付きで保存できる |

価格下落、経過期間、単なる回復遅延、新規買いfloor未達、Target到達、他候補や集中warning単独では売らない。新しい買付候補がなくても現金を回収できる。active提案にadd/reduceは持たないが、実際の追加購入・部分売却はledgerへそのまま記録する。

残存見返りは現在から保有する増分で判断する。受取済みcashと権利確定済み未入金分を将来増分へ二重計上しない。旧horizonの短縮で年率を水増しせず、更新した企業評価と税・費用の不確実性を文章で扱う。専用の税engineや配当状態storeは作らない。

## 対象限定の入力確認

新しいPosition Review Operationは作らない。activeな資本調査と並行して対象holdingを扱える。`position-prepare`は保有実在を確認し、全holding quoteの事前ledger applyやappend head一致を要求しない。

buildは対象holding、最新Reviewed Thesis、対象quoteを読む。作者は`remaining_reward`だけを記入する。`action`はcheckで導出し、confirmed publishのtransaction内で再計算して保存する。入力したactionは判断元にせず、同内容retryは保存済みactionを返す。check/publishは提出したremainingを保持したまま必要入力だけを再確認する。数量・cost・重要なbasis・quote・latest Thesisが変われば再確認を求める。無関係な入出金や他ticker更新だけでResearch全体をやり直さない。

buildの標準出力とcheckは、現在quoteを共通算術へ渡した`current_price_projection`を見せる。Base/Downsideの総return・年率、期間、累積分配、価格basisを原評価から分け、draftとcanonical payloadへ複写しない。quote・権利単位・同日評価が未確認ならnullとする。この値を固定exit閾値にはしない。

権利単位を確認できないholdingに数量付き売却案は付けない。nullはhold推奨でも実売却でもなく、exit候補もbroker約定ではない。publishには明示的な`--confirmed`が必要で、確認なしでは書き込まない。

<a id="commands"></a>

## 操作

作成・check・人間確認・publishと旧Thesisからの更新は[Position Review skill](../../.agents/skills/position-review/SKILL.md)が所有する。現行Position Reviewはapplication DBへ保存し、旧payloadとOperation kindは履歴として読む。旧版を新しい判断として実行しない。

数量basisは現在の保有episodeからの権利変化情報で確認する。過去終値だけの欠損は数量不明の理由にせず、対象row・調整係数の欠損や権利変化は未確認のままにする。現在quoteの有無は別に確認する。
