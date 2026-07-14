# 詳細research統合report 運用テスト（#423）

## 0. 目的と採用判定

詳細research後の比較・購入方法を、レビュー可能な小さい構造化情報へ統合し、内容レビューに合格した
情報だけをHTMLへ投影する運用が、投資判断の誤読と出典driftを減らせるかを確認する。

採用条件は次のとおりである。

- 指定銘柄ごとの質問、事業、成長品質、財務耐久性、反証、scenario、FV、sourceを一つの
  `findings.yaml` で比較できる。
- 購入候補はcanonical decision packet、独立review、portfolio ledger、raw closeへhashで結び付く。
- HTML生成前に、独立second passが内容を8観点で確認し、差し戻しをfail-closeできる。
- HTMLはrepository専用rendererで再生成でき、外部script・外部asset・任意URLを埋め込まない。
- 新しいpublic CLI、永続schema、dependency、broker連携を増やさない。

この運用テストは上記を満たしたため採用する。HTMLは正本ではなく、review済み構造化情報の
閲覧用projectionとする。

## 1. 入力とprovenance

| 入力 | 値 | 用途 |
| --- | --- | --- |
| repository baseline | `056a5f62962b47d283ff2d757d9fd844c4ae1263` | code / contractの基準 |
| price as-of | `2026-07-14` | raw execution close |
| disclosure latest | `2026-07-14` | local market cache freshness |
| findings SHA-256 | `2863cdbd5391e8d40ee773ed3d42295384fd0ba0568b8023841a82365f3792c1` | 統合research本文 |
| comparison SHA-256 | `34c54e4248ec395d0d1188508a67e9ed4a9764aebc07cbed7e880d088acd178d` | 3社比較と選定 |
| proposal SHA-256 | `2ff8d1d3a63981b65fe40d824f6b706bb3df269bdc05e4826f245c43d5a1bbf3` | 指値・数量・期限 |
| selected packet core SHA-256 | `c921db2547329a5bdc0d9c68bfd1e7297ff473c44bdad628ac8490843ff4cce3` | 3836のreview対象本文 |
| selected review SHA-256 | `f9567efeee8db97ec36dca518381f38dc9e33026a7662b4daa018e829873afb3` | 3836の独立反証 |
| ledger SHA-256 | `c804463c75bf95cbfc6f3dda21dfc96da95f1c9a8034d4faae6ffe437809b55f` | sizing前portfolio |
| integrated content review SHA-256 | `7a98926e7387af2997f79fe97d0c424f2f6c6be5d9ba5b41cfa25fcef4643ca4` | HTML前の独立second pass |
| HTML projection SHA-256 | `b5f4abc59ba2875ab480f0e7e2da7d89017ed8f830a8dc82fbf69d6f42df660e` | review済み表示物 |

`findings.yaml` はlocal working artifactであるため、その内容をGitの正本とはしない。選定した3836の
decision packetと独立reviewだけを`records/03-thesis/`へ昇格し、report reviewはhashによって
local inputsを固定する。

review済みbundleはprivate operation Issue #413へ、manifest、findings、comparison、3659 / 3901の
non-promoted packet、proposal、report reviewをartifact別commentとして保持する。selected 3836のpacket /
reviewはcanonical pathとhashを参照し、Issueへ同じ内容を複製しない。

## 2. 3社の比較結果

| ticker | 5年base CAGR | required return | FV / raw close | 永久損失 | 判断 |
| --- | ---: | ---: | ---: | --- | --- |
| 3659 ネクソン | 8.59% | 10.00% | 2,119.77円 / 2,261.50円 | elevated | defer |
| 3901 マークラインズ | 7.99% | 8.00% | 1,382.04円 / 1,383.00円 | unknown | defer |
| 3836 アバントグループ | 12.58% | 8.00% | 1,481.91円 / 1,204.00円 | acceptable | select |

3659は既存IPと現金・預金による短中期耐久性を持つが、新作成功込みのcash flowを恒久run rateへ
外挿せず、blockbuster不在と資本配分riskを10%要求利回りへ反映した。3901は商品coverageと海外拠点を
持つが、ultimate originでは日系関連売上が約70%で、総契約・海外契約の減少を価格改定とFXが覆う
状態から非日系ローカル顧客獲得の再現性を確認できない。3836はsoftware転換が未証明でも、会計BPOと
連結決算基盤、DX segment、自社software比率の上昇余地を保守的scenarioへ落とした後も要求利回りを
上回る。

3836のstrongest countercaseは、DX需要が伸びても人員集約SI/BPOに留まり、software gross profitと
一人当たり利益が加速しないことである。terminal 12倍、shares横ばい、earnings成長7%のstressでは
5年CAGR 8.97%を維持するが、成長5%では6.88%へ下がるため、FY2028目標の売上達成だけでなく
software gross profitと収益性を継続確認する。

## 3. 購入方法

3836を`2026-07-15`の東証立会時間中に、**1,204円の指値で200株、最大240,800円**として提案する。
期限は`2026-07-15T15:30:00+09:00`、許容上限は1,481円である。許容上限は成行を許す価格ではなく、
1,204円を超えて未約定なら今回の注文を追いかけない。注文入力と約定記録は人間確認後の別工程であり、
本reportはbroker操作を行わない。

購入後の推定ticker exposureは4.84%、情報・通信業sector exposureは22.73%で、policy制約内である。
ledgerのmarket valuationは2026-07-10時点のため、注文直前にcurrent / reserved exposureを再計算する。

## 4. 内容review gateの実証

review対象はHTMLではなく、`findings.yaml`、comparison、3社packet、selected review、proposal、manifestで
ある。独立second passは次の8観点をすべて確認する。

- source freshness
- user questions answered
- primary source traceability
- fact / derived / estimate separation
- countercase and unknowns
- scenario and FV consistency
- comparison and portfolio fit
- purchase method binding

初回reviewは、3659のcash flow判断と3901・3836の財務耐久性で観測値と推論が同じ`observed`記述に
混在することを検出し、`changes_required`でHTML生成を停止した。観測事実とestimateを分離した後、
新しいfindings hashに対する独立reviewを要求する。この差し戻しは、review gateが形式上の承認ではなく、
事実・分析分離の欠陥をHTML公開前に止めることを示す。再reviewは8項目すべて`pass`で、18 scenario、
3社のFV、比較順位、3836の指値・数量・予算・packet / review / ledger hashを再計算・照合した。

## 5. 専用rendererとsecurity boundary

HTMLは次のrepository commandだけで生成する。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python -m tools.research_decision_report.render \
  --workspace .cache/opportunity/2026-07-14 \
  --findings .cache/opportunity/2026-07-14/findings.yaml \
  --review .cache/opportunity/2026-07-14/report-review.yaml \
  --proposal .cache/opportunity/2026-07-14/3836/proposal.yaml \
  --out .cache/opportunity/2026-07-14/research-decision-report.html
```

rendererはreview conclusion、8 checks、全input hash、selected ticker、packet / review / ledger hash、
proposal期限をfail-closeで照合する。本文をescapeし、CSPを付け、TradingView URLはtickerから固定形式で
生成する。ブラウザ自動起動skillや汎用HTML生成skillは使わない。

## 6. 改善項目の反映先と残る制約

今回の運用で検出した次の課題は同じ変更単位で解消する。

- 詳細research・比較・購入方法を共通templateへ統合する。
- HTML前のcompact情報へ必須の独立内容reviewを置く。
- decision packetのraw / core hash、独立review hash、ledger hashを指値proposalへ固定する。
- opportunity statusをlane進捗に合わせ、比較可能前の早すぎるready表示を防ぐ。
- screening参考価格とraw execution closeを別labelにする。
- user質問、business model、value capture、growth quality、financial resilience、domain findings、unknownsを
  templateの必須構造にする。
- source statusとchecked-atを持ち、一次情報未取得を事実のように表示しない。
- CSP、escaping、固定link allowlistでHTML出力境界を守る。
- ticker URL表示と重複するbrowser自動起動skillを削除する。
- 汎用HTML skillではなくrepository専用commandをrunbookの正本にする。
- review済みcompact inputsをhashとlocal pathだけで失わず、operation Issueへ内容をartifact別に保持する。

新surfaceは反復運用を確認するまで`tools/`とlocal artifactに留める。public CLI、永続的なreport schema、
broker連携は追加しない。2〜3回の実運用でtemplate不足または手入力driftが反復して確認された場合だけ、
stable CLIやrecords schemaへの昇格効果を再評価する。
