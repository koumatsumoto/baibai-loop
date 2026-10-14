---
title: "Screeningの履歴条件と旧設定の見直し"
summary: "182日共通floorと未使用YoY閾値を整理し、固定L1でのv6再現と長期較正の成立範囲を検証する。"
doc_type: measurement-record
status: historical
---

# Screeningの履歴条件と旧設定の見直し（#1481）

## 目的・採用理由

価値tierはT1。短いIPO価格履歴と必要財務履歴を混同した一律除外、機能廃止後の設定、機械推計と企業判断の要求率の混同を除く。推薦数・取引数・利益増を完了条件にしない。現在のルール分類、理由、適用先と根拠区分は[Screening reference](../../../docs/reference/screening-runtime.md#rule-purpose)が正本で、この記録は当時の比較・採否・検証だけを残す。

上場6か月未満の除外は`b7bb0d4e`（2026-04-24）の「IPO直後の値動き特殊性」に由来し、`911a2515`で最古barによるproxyへ、`b10fc463`で分析層へ移った。182日の経済的最適性は確認できない。現在の4 Approachには20bar scope・必要な利益/資産/CF入力が別にあり、価格履歴の日数は実上場日や必要財務履歴ではない。親はfloor解除を採用した。短い価格履歴の不確実性を消す変更ではなく、Researchへcontextを渡す。

`quality.yoy_deterioration_threshold`は`73aa45d6`の旧判定撤去後もschemaとrules/hashに残り、現在のconsumerはなかった。現行modelと新rulesから除いた。evaluatorの固定-30%は診断群を分ける事前固定帯として保持する。run warningの「業績悪化フィルタ入力欠損」も「業績YoY入力欠損」に直した。

Thesisの通常算術例はr=6%へ更新した。これは#1464の暫定政策との整合で、最適性・株主資本コスト・期待収益の実証ではない。機械E[r]の固定8.5%帯、収束係数、clip、既存評価のr/Pmaxは変更していない。Downsideが6%を満たす追加購入gateも作らない。

## 維持した境界と機会費用

| 対象 | 維持理由・残る限界 |
| --- | --- |
| 必須fact・JPX・20bar scope | 未観測と無規制、計算不能と経済的見送りを分ける。欠測を0で補わない |
| Current / Normalized / Asset / Reinvestmentの資格・順位 | 意味の合うnative座標で異なる価値仮説を発見する。金融資産proxy、連続FY、正の分母、相対qualityの適用境界を守る。機械的資格外を企業rejectへ読み替えない |
| top20・最大3件 | 発見入力と作者/独立Reviewの運用容量であり、経済的最適値ではない。同じ保存入力でtop10/20/30のunionは39/77/113件。枠外機会は①の明示Researchの課題と分ける |
| TTM期間一致・入力窓・median n<10 fallback | 別期間・別basisを混ぜない算術契約と計算慣例。値の最適性と再現テストの成功を混同しない |
| Research期間・評価同日 | 期間は正整数で、旧12〜36月等の上限は現行code/active skillに残っていない。評価同日は受取済み分配と将来増分の混同を防ぎ、差分更新で扱う |

## 固定入力と正式Screening再現

観測比較の基準mainは`bb65679d7021c225b6773cb3591f546d81a04d96`。実装は`86c7c2dff36fba34369218601d90ba37196fe901`へrebaseし、#1485/#1486の台帳・未約定の意味はPortfolio Ledgerの正本へ参照で繋げた。観測日は2026-10-13、作業日は2026-10-14 JST。①ResearchSelection/CAA、②CB算術、#1480全面モデル再設計は実装していない。

既存read-only経路でmachine bundleをpullし、固定L1 release `20261013T121019Z-2ae2e9de-1c54ef604f62`（manifest SHA-256 `c1fe3581e55bfdc4ee5c396996829abf701f60111d16090f3d0ba57416f5d32c`）を正式hydrateした。全512 objectのdigestとrow countを検証し、providerの新規取得・AI呼出・application DB読取/書換・本番writeは0。原runs storeの5runは不変。

最初に保存済みv5の3,680解析から77件のReview Setを全fieldで再現した。182日未満14件のうち13件はcapも欠測で、唯一他の共通条件を満たす604Aはbar期間76日だった。保存された丸め済み値による局所比較では適格+1、候補差分0となった。その局所比較とは別に、固定L1の未丸め入力からcache-onlyの正式runを検証用storeへ生成した。

| 指標 | 本番保存v5 | 固定L1の正式v6 |
| --- | ---: | ---: |
| Security Analysis | 3,680 | 3,680 |
| common eligible | 3,495 | 3,496 |
| 各Approach Nomination | 20ずつ | 20ずつ |
| Review Set | 77 | 77 |
| 追加 / 退出 | — | 0 / 0 |
| Nomination・順位の変化 | — | 0 |
| candidate analysis contextの変化 | — | 3件 |

比較母集団の変更は推薦集合だけでなく相対座標も動かす。全解析ではPER/PBR/P/Sのgap、FV sector anchor、sector relative strengthが変わり、E[r]は43件で変わった。機械係数を変更した結果ではない。候補が同じという事実を、母集団変更が無害・無意味という一般的結論や利益改善の実証へ広げない。

v6は`multi-valuation-v6`、Approach IDsは維持。production rules hashは`8af4e0fbbcc0b736`、Candidate Discovery method hashは`e1383b0198e248736684aa6001d1e63220670549c75b31e278855a8ae1c34b69`、calibration method hashは`dab39de9fccb070c`。正式validatorで新Review Setの全field一致を確認した。partial warning（TTM品質・YoY欠測）は保持した。

旧artifactのhash・統計・生成日と凍結rulesは変更していない。未使用fieldが元hashへ含まれていたため、CIの来歴検証だけで旧fieldを復元し、原hashを検証する。runtimeの旧算式・二重較正・旧payload書換を追加しない。現行runへの適用は引き続きreaderの完全一致を必要とする。

## 較正の測定計画と不足

結果確認前に、全83月次cohort（2019-11-29〜2026-09-30）を診断し、E[r]水準とCandidate Discovery unionのrequired scopeを旧common windowに含む**全19月末**（2020-01-31〜2021-07-30）へ固定した。旧artifactのcommon cohortは18件で元の除外月はartifactから復元できないため、成功月だけを選び直さない。context評価もstart/endでこの19件へ限定する。

v5の途中buildは採用方針決定後に停止し、途中行を新hashとして再利用していない。最終v6で同じ固定L1を用い、正式`calibration-build --force`を別の検証storeへ実行する。旧canonical較正store・公開artifactを先に削除せず、現行codeのtemporary buildとatomic replaceを使う。

```bash
uv run baibai-engine screening calibration-build \
  --start 2019-11-01 --end 2026-09-30 \
  --calibration-dir .cache/1481-calibration-v6 --force
```

E[r]水準は`estimator_policy`のmandatory `er_calibration`に`er_level_calibration`を追加する。両方の3y/5yと全19 required cohortが成立した場合だけ、正式`--context-out`で生成する。scopeが不成立ならreasonと未解決件数を残し、公開artifactのhashを付け替えない。Candidate Discoveryは同じscopeで`candidate_discovery_nomination_union`を選び、4 Approachとunionのfidelityを必須にする。

価格・財務履歴は2016-08以降を持つが、EDINET metrics・JPX規制sourceは2026-05-08以降である。最古完全cohortが2026-05なら、3yは2029-05以降、5yは2031-05以降の満期を要し、source completenessも別に必要である。E[r]の分布が成立しても、新4 Approachの長期優越性や個別企業の採否は実証されない。満期未到来を実装未完とは呼ばず、現在可能な生成・不足検証を区別する。

## 較正の最終結果

最終v6で83cohortを再構築した。panelは312,774行、forwardは1,564,285行（resolved 1,097,033行、観測した現金支配権exit 5,580行、破綻型exit 1,031行）。全83cohortのforwardが保存済みで、`integrity_check=ok`、foreign key違反0、全panelのmethod hashは`dab39de9fccb070c`だった。件数はticker/as-of/horizon行であり、独立した会社・イベント数ではない。集計は[calibration-validation.json](./calibration-validation.json)に残した。

| 固定scopeの評価 | 3y | 5y |
| --- | ---: | ---: |
| required cohort | 19 | 19 |
| er_calibration / er_level_calibrationのmetric eligible | 各19 | 各19 |
| integrity eligible / blocked | 17 / 2 | 18 / 1 |
| entry year 2020 / 2021 | 12 / 7 | 12 / 7 |
| 重複しないforward window | 1 | 1 |

**E[r] contextは生成しなかった。** 正式evaluatorのrequired scopeは`evidence_complete=false / blocked`で、`--context-out`はexit 1・`evidence is not complete`で書込を拒否した。通常の評価artifact出力自体はexit 0で成功したが、evidence成立や採用を意味しない。失敗した月を除外し、35組の成功だけで新contextを作ることはしていない。旧published artifactは元bytes・hash・生成日のまま保持した。

具体的な不足は次のとおり。

- 2020-06-30の3y/5y: `entry_price_gap`。7531の最新non-null closeは2020-06-10で、entry許容15日より古い20日前だった。6月末までの日次rowは存在するがcloseはnullで、単にrowがないという説明は誤りである。7351の未上場entryとは分類が分かれている。より新しい正のcloseを裏付ける保存事実が必要であり、古い価格の持越し・0補完・日数gateの拡大はしない。実際の無売買かsource欠落かも、今回の保存値だけでは断定しない。
- 2021-04-30の3y: `unpriced_exit_flips_direction`。stale exitは216行、delisting除外は205行。Review Setのmedian excessは報告値+5.7887pt、全損置換+0.9771pt、中立置換0ptで、既存の「正 / 非正」判定が揃わない。E[r] spreadの方向自体は安定しているが、全体integrityの条件は結果確認後に変更しない。未価格exitの価格・廃止理由・対価の裏付けを整え、同scopeで再生成・評価できるまで不足を残す。これら置換値を観測実現returnとして保存しない。

**4 Approach unionの長期evidenceもblockedである。** required scope全19cohortでJPX入力がunavailable、Asset/Reinvestmentの長期入力を満たさず、全4 Approach/union fidelityがunresolvedとなった。全83cohortのJPX入力は78 unavailable / 5 completeで、3y/5yに成熟したfull union fidelityは0。全期間の3yは47 resolved cohort・非重複窓2、5yは23 resolved cohort・非重複窓1であり、月次cohort数を独立標本数に読み替えない。

現在可能な正式生成・評価・context拒否・不足検証は完了した。追加取得・課金・制限回避・scope変更は0。新係数や新screenの利益優位を主張していない。残る価格/exit・長期fidelityの不足は測定上の制約で、採用済みルール修正の実装未完ではない。

## 検証と反映境界

関連回帰は短履歴のNomination/context保持、欠測/非有限/JPX除外、production/calibration共通母集団、旧artifactの原identityを含む。完全local gateはPython 4,207 passed / 8 skipped、coverage 87%、frontend 295 passed、Worker 93 passed、Ruff/mypy/import-linter/drift/Bandit/runtime・build依存監査/Node監査/Wrangler dry-runが成功した。

実データコピーがあると既存Researchテストの一部が既定pathを読んだため、fixture gateでは取得コピーを一時的に隔離し、後で同じbytesを復元した。環境に残る退場packageのbytecodeだけも除去した。テスト条件やruntimeのvalidationを緩めず、fixture gateと固定L1の実データ検証を分けた。指標storeのschema v7・122系列・459,859観測の検証も成功。application storeは取得しておらず、同storeの検証は未実行。

ルール修正は親レビューとCI後にPR #1487としてmain `cb748541d6d67fe059ee020e0d1a588783e948b4`へmergeされた。この追記PRは測定結果だけを扱い、本番Screeningの反映は別の正式publicationと受入で確認する。#1481は①②および横断点検の残作業があるためcloseしない。個人の保有・損益・非公開Research本文は記録していない。
