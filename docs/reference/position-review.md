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

## Schema v3

`position_review_id / thesis_id / position_id / ticker / as_of`、必要なholdingのquantity/cost/basis、quoteのprice/observed_at/basis/source、`remaining_reward`、`action`、`unresolved_reason`を持つ。remaining_rewardはsufficient/insufficient/uncertainと経済的なreason、未評価ならnull。企業評価全文、代替候補、追加購入context、元本に対する損失率は持たない。

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

buildは対象holding、最新Reviewed Thesis、対象quoteを読む。作者がremainingを埋め、check/publishは提出したremainingを保持したまま必要入力だけを再確認する。数量・cost・重要なbasis・quote・latest Thesisが変われば再確認を求める。無関係な入出金や他ticker更新だけでResearch全体をやり直さない。

権利単位を確認できないholdingに数量付き売却案は付けない。nullはhold推奨でも実売却でもなく、exit候補もbroker約定ではない。publishには明示的な`--confirmed`が必要で、確認なしでは書き込まない。

<a id="commands"></a>

## 操作

[Position Review skill](../../.agents/skills/position-review/SKILL.md)が手順を所有する。利用可能な最新v4の更新には`thesis-scaffold --from-thesis-id`を使い、元資料・予測からの差分を再Reviewする。旧版しかない保有は同引数なしで新規v4を作り、現在の証拠と独立Reviewを揃えて旧IDをsupersedeする。現在の`--help`で引数を確認する。

```bash
uv run baibai-engine research position-prepare --help
uv run baibai-engine position position-review-build --help
uv run baibai-engine position position-review --help
```

既存schema20のposition_reviewへv3を追加する。旧payloadとOperation kindは履歴として読み、新規判断へ実行しない。実売却は人間の報告後にledger-recordで記録する。

数量basisは現在の保有episodeからの権利変化情報で確認する。過去終値だけの欠損は数量不明の理由にせず、対象row・調整係数の欠損や権利変化は未確認のままにする。現在quoteの有無は別に確認する。
