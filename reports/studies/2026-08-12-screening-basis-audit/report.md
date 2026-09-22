---
title: "screening基盤の反証監査"
summary: "株式・期間・時点・合成basisを実データとproduction entrypointで反証し、5つの実データ欠陥と2つの境界契約を修正した。"
doc_type: measurement-record
status: complete
date: 2026-08-13
---

# screening基盤の反証監査

価値tier: T2 — 資本状態、corporate action、会計期間のbasis不整合が時価総額・倍率・E[r]・順位へ入る経路を止め、一次researchへ渡す候補の正確性を守る。

## 1. 結論

事前登録したS1〜S9を、2026-08-12の実データ、market store全履歴、production `screening run` / `select`、81 cohortの3y / 5y較正で再検査した。実データで判断面への到達を確認したcorrectness defectは5種類である。

1. 正の`TrShFY`の後に新しい`ShOutFY`だけを観測したとき、旧自己株を再控除していた。
2. `AvgSh`をgross issuedのfallbackとして保存し、正の自己株を二重控除できた。
3. `total_assets`と`EqAR`を別の資本状態から合成した。
4. `close = NULL`のcorporate-action eventをprice bar readerが消し、6731の100株→1株併合を全経路から失った。
5. 同一会計期間の部分訂正で営業利益が空欄になると、より具体的な既報営業利益を捨てて純利益へfallbackした。

加えて2つの境界契約を防御的に揃えた。direct providerはinstalled ClientV2の短縮period fieldをSQLite writerと同じ意味で読む。`build_universe`は入力に混じったas-of後のbarを入口で除く。前者は実API credentialがreview環境に無くfixtureとinstalled client contractによるparity確認、後者は実store到達0件でfuture-row injectionによる境界mutationであるため、実データ到達済みdefectには数えない。

最終コードで較正storeを81 cohortすべて再構築し、固定15 as-of × 3y / 5yの30 authorityを再評価した。30/30がeligibleで、`production_change_allowed: true`である。続けて実storeからpublic `screening run` / `select`を実行し、3,706 candidate、1,554 liquid、1,552 ranking-eligible、top 5は6417、6458、7595、3836、5445となった。issue開始時のrunと比べてtop 5とtop 20は不変、top 100は99件重複した。

一方、遅延訂正を使ってsnapshot、TTM、forecast全体をperiod順に復元する案は、実データ較正で30 authority中2件がdirection-sensitiveなunpriced exitとなったため採用しなかった。同日複数documentの復元とNaN current forecastから`Nx*`へのcross-period fallbackも、schema 23の保存済みraw provenanceでは証明できないため`insufficient`とした。レビュー上の可能性だけではproduction変更を採用していない。

閾値、playbook、E[r]式、FV policy、DB schemaは変更していない。market storeの書換えとcloud pushも行っていない。

## 2. 入力と再現identity

判断に必要な実測値・採否・限界は本書を記録とする。実行時のmachine artifactは完了済みstudyのraw出力であり、HEADには保持しない。

| 入力 | identity / coverage |
| --- | --- |
| baseline commit | `ec86eaebc8a382257206d6b2bec4dfed3b637023` |
| preregistration commit | `c73aaed0` |
| production as-of | `2026-08-12` |
| market store | schema 23、SHA-256 `5bf8e58642b41e404e617dcc21a5872cb02f8df4265c8aa54594fd03f19a9875`、`quick_check: ok` |
| financial summary | 182,314行、2016-08-01〜2026-08-12 |
| daily bars | 10,136,873行、2016-08-01〜2026-08-12 |
| corporate-action event | 2,597行、1,957銘柄、2016-08-29〜2026-08-07 |
| final run | `run-revision-468dc6e6c13145bea2d4f5ea6ea1fe54` |
| final selection | `selection-6c2b3eabda104592bc67edc275377461` |
| rules runtime hash | `05b14095834171be` |
| calibration | panel 81 files / 305,367行、forward 81 files / 1,527,240行、0 byte 0件 |

中断した`.calibration.rebuild/`は最終コードで最初から作り直し、完成後のatomic swapでcanonical `calibration/`へ置いた。`calibration/`に誤生成されたリテラル名`*.sqlite`の0 byteファイルはcanonical storeから隔離し、削除せず`.cache/941/quarantine/`へ退避した。

run / selectionはlocal artifactへ出力し、application DBへpublishしていない。比較対象のissue baselineは`run-revision-7d824fb6e4444833bc795a84cd62726f` / `selection-817e5787228e4ec58158e26db6bbe3bb`である。

public runはtyped source warningを伴うpartialとしてexit 2を返し、candidate artifactは正常に生成した。内訳はTTM non-exact 10,416行、liquidかつplaybook-requiredでTTM non-exact 1,109行、deterioration gate unmeasurable 41行、buyback unreadable 22行 / 20銘柄である。これらを0件や成功扱いへ潰さず、そのartifactに対する`select`はexit 0で完了した。

## 3. S1 — capital basis

### 3.1 `TrShFY`空欄後のfail-close

`TrShFY`はcapital-state factだが、J-Quantsの空欄は0株と未報告を区別しない。正の自己株のsourceより後に`ShOutFY`だけを観測し、その後に新しい`TrShFY`を観測していない場合、旧自己株が現在も有効か判定できない。この状態はgross issuedの増加・減少・不変を安全条件にせず、`indeterminate_positive_treasury_after_later_issued_observation`として時価総額と派生倍率をfail closedにする。新しい`TrShFY`観測（明示0を含む）で再開する。

current断面では20銘柄がこのreasonで停止し、全件がpopulation外になった。81 cohortの全実行状態ではcapital failureは1,064 ticker-cohort / 113銘柄だった。

| reason | ticker-cohort | 銘柄 |
| --- | ---: | ---: |
| `indeterminate_positive_treasury_after_later_issued_observation` | 989 | 96 |
| `indeterminate_share_basis` | 17 | 6 |
| `invalid_issued_or_treasury_shares` | 9 | 3 |
| `invalid_treasury_source_capital_basis` | 21 | 1 |
| `issued_matches_average_with_positive_treasury` | 28 | 9 |

正の旧自己株後に新しいissuedを観測した状態は1,010件で、issued減少625、増加313、不変72だった。resolverがこの状態をfail-openした件数は0である。split basisが判別不能になったbarrierは343状態あり、新しい確定capitalで323状態が再開、20状態がfail closed、barrier以前のbasisを実装が復活させた違反は0だった。

6184と7049は、空欄の意味を0株と断定せず「旧自己株が継続するか判定不能」という契約の実例として扱う。4889は一次資料上は自己株10株の継続が確認できるが、保存済みNULL入力だけでは証明できないため停止する既知のavailability costである。したがって989件をすべて実際の自己株処分と数えない。

### 3.2 `AvgSh`とgross issued

全182,314 summaryで`shares_outstanding == average_shares`は2,743行、さらに正の自己株があるものは137行だった。等値だけでは発行済が動かなかった正常1Qも止めるため、既存cacheでは`issued == average`、正の自己株、`issued + treasury == 過去gross issued`の隣接恒等式が揃う場合だけfail closedにする。direct / SQLite writerから`AvgSh` fallbackを除き、将来行はgross issuedへ期中平均を保存しない。

4167では保存値7,563,857株と自己株352,373株の和7,916,230株が前後のgross issuedと会社開示に一致する。単純な`ShOutFY == AvgSh` guardは正常行を過剰除外したため採用しなかった。

## 4. S2 — corporate action

全履歴の`adjustment_factor != 0, 1`は2,597行 / 1,957銘柄で、そのうち`close = NULL`は56行 / 51銘柄だった。production窓2023-04-30〜2026-08-12では772行 / 721銘柄、`close = NULL`は22行 / 22銘柄である。旧readerが見ていた750行 / 701銘柄はclose付きsubsetにすぎなかった。

6731は2023-12-27にfactor 100、close NULLを持つ正規のaction eventである。price barとeventを分離し、run、universe、metrics、panel、shareholder-return、forward、read API、market snapshot、regime、ticker profileへ同じevent集合を配線した。event自身はprice session数やADVへ混ぜない。

| 6731、2024-01-31 panel断面 | event喪失 | 修正後 |
| --- | ---: | ---: |
| market cap（億円） | 1,172 | 12 |
| PBR | 222.2098 | 2.2221 |
| P/S | 80.7429 | 0.807429 |
| 60日price return | `87.5`（+8,750%） | `-0.115`（−11.5%） |
| population | true | false |

2023-11-30断面の6か月forward price returnは、event喪失時の`44.0`（+4,400%）から修正後の`-0.55`（−55%）へ変わる。2024-01-31断面ではactionがentryより前なので、正しい6か月returnは`-0.5028248588`であり、eventを除いても変わらない。

JPXは12月27〜28日の売買停止と29日再開を公表し、会社開示は100株→1株、効力日2023-12-29を明記する。この一次情報と、実storeのfactor 100 / close NULL、前後価格、production panel / forwardの双方が一致した。

## 5. S3 — periodと普通株自己資本

### 5.1 独立恒等式

| 検算 | n | 1%以内 | p50絶対残差 |
| --- | ---: | ---: | ---: |
| FY profit ÷ `AvgSh` とreported EPS | 39,586 | 95.22% | 0.0318% |
| `BPS × shares_ex_treasury` とequity proxy | 38,373 | 96.87% | 0.0499% |
| FCF = OCF − capex | 2,942 | 100% | 0円 |

残差tailだけを根拠に別entity、別period、普通株自己資本と純資産を同一視していない。

### 5.2 `TA × EqAR`のsame-state化

普通株自己資本の円経路は、`total_assets`と`EqAR`を同時観測した最新行だけから作る。near-zeroの`EqAR`は小数第3位、BPSは小数第2位の公表精度を区間として比較し、相対誤差だけで整合を拒否しない。円経路が新しいBPSより古い場合はBPS経路へfallbackする。

81 cohortでは独立carryしたsourceとsame-state sourceが違う状態が254 ticker-cohort / 66銘柄あり、PBRを正値で出す経路の出力変更は196状態 / 47銘柄だった。absolute relative changeはcommon equityでp50 2.60%、p95 12.06%、PBRでp50 2.67%、p95 13.71%。41状態がpopulation、3状態がtop 100、2状態がrecommendedに到達した。9628のcurrent実例は同一行の組より自己資本を28.6%過大にするcross-state合成だった。

### 5.3 period境界

direct providerはinstalled ClientV2 contractの`CurPerType` / `CurFYEn` / `CurPerSt` / `CurPerEn`をSQLite writerと同じ意味で読む。このreview環境では実API payloadを取得できなかったため、実データ到達済みdefectではなくdirect / SQLite parityの境界防御として扱う。同一periodのactual fallbackは、そのperiod内で営業利益、経常利益、純利益の順に各fieldの最新nonnullを解決し、より新しい部分訂正に営業利益が無いだけで純利益へ落とさない。

開示日latestとperiod-first current actualの診断差は408 ticker-cohort / 142銘柄だった。ただしsnapshot、TTM、forecast全体をperiod順へ変える候補実装は固定3y / 5y authorityの30組中2組をblockした。実データで原因を分離し、採用範囲をperiod alias、capital state、same-state equity、同一period field fallbackへ限定した。schema 23が保存しない同日document identityを解決したとは主張しない。

## 6. S4〜S6 — boundary、as-of、E[r]

### S4. population boundary

raw market capが100億円未満でも保存後100億円となる行は3件、raw 20日平均売買代金が1.0億円未満でも保存後1.0億円となる行は22件だった。raw→保存値のround-trip不一致は0。保存済みrounded factをthreshold basisとする現行contractに一致するため変更しなかった。listing spanはnull 0、182日ちょうど0、182日未満24件である。

### S5. as-ofとrevision

current candidateに`disclosed_at > 2026-08-12`のfinancial rowとfuture EDINET rowは0件、最大開示日は2026-08-12だった。一方、production関数へas-of後のbarを注入するとmarket capとtrailing turnoverが変わることを再現したため、barを`traded_at <= asof`で入口filterする。修正後はfuture barを加えてもoutput不変である。実storecurrentにfuture bar到達は0件なので、current funnel影響は0である。

schema 23のfinancial summaryは`(ticker, disclosed_at)`で1行しか保持せず、同日相補documentやrevision identityを復元できない。これは`insufficient`であり、schema変更や推測合成は採用しなかった。

### S6. E[r]

current 3,706 candidateのE[r]非nullは3,637件。保存artifactの丸め後に独立再計算したannual成分の最大絶対残差は0.0001だった。最終較正81 panelではE[r]あり298,661件、annual成分の最大残差`5.55e-17`、carry成分0である。

dividend欠損614件、net-share-change欠損131件の0写像は明示model contractである。ranking-eligibleではそれぞれ83件、36件、top 100のshare-change欠損は1898と2130、dividend欠損は0件だった。反実仮想のrow除外はranking-eligibleを115件除くがtop 20を変えず、unknown化は順位を定義できない。policy変更の根拠がないため0写像を維持した。

## 7. S7〜S8 — gateとfield reachability

final runのevidenceは1,577行 / 1,316銘柄。`cashflow-yield-discount` hitのFCF欠損は82件、liquidかつranking-eligible 59件、sole evidence 51件、top 100は7267 rank 20、3405 rank 25、9755 rank 86、4114 rank 90だった。`fcf_yield_required_positive`はFCFが観測された場合に非正を拒否するcontractで、FCFそのものをrequiredにしないため変更しなかった。

全3,706 candidate × 4 playbook = 14,824観測をproduction evaluatorで分類した。

| playbook | true | false | unknown |
| --- | ---: | ---: | ---: |
| cash-rich-asset-discount | 199 | 1,447 | 2,060 |
| cashflow-yield-discount | 242 | 848 | 2,616 |
| sales-discount-growth | 661 | 2,484 | 561 |
| valuation-reversion | 475 | 0 | 3,231 |

`operating_profit_yoy` unknownのhitは2件で、いずれもcash-rich、unmeasurable annotation付き、annotation欠落は0件だった。

112 fieldのcardinalityを測り、all-null 5 field、nonnull constant 9 fieldを記録した。reader / writerの到達は確認したが、全112 fieldのsemantic mutation対応表までは証明していないためS8は`insufficient`である。

## 8. S9 — production run / selectと較正

issue baselineと最終実行の比較は次のとおりである。

| 段階 | baseline | final | 差 |
| --- | ---: | ---: | ---: |
| structural candidates | 3,706 | 3,706 | 0 |
| liquidity通過 | 1,558 | 1,554 | −4 |
| E[r] ranking eligible | 1,556 | 1,552 | −4 |
| evidence annotated（liquid） | 378 | 379 | +1 |
| recommendations | 5 | 5 | 0 |
| longlist | 100 | 100 | 0 |

PBRは24銘柄、E[r]は60銘柄、evidence membershipは19銘柄で変わり、E[r]最大絶対差は0.0641だった。top 20は20/20が同順位、top 100は99/100重複し、5851が外れて4023が入った。共通99件の最大rank差は1、top 5は不変である。candidate payload全件差にはcalculation revisionやannotationの更新も含むため、その件数をdecision impactとは読まない。

較正storeは最終rules hash `05b14095834171be`で81 cohortすべてを再構築した。panel 305,367行、forward 1,527,240行、resolved 1,059,521、control-event exit 4,686。固定15 as-of × 3y / 5yの30 metric-cohortは30/30 eligible、blocked 0で、published calibration contextも同じhashへ更新した。

## 9. mutationと隣接反証

採用した各変更は、変更点だけを戻すmutationで旧出力が再現し、隣接正常系を壊さないことを固定した。

- close=NULL eventを外すと6731のmarket cap、PBR、P/S、60日return、6か月forward returnが100倍basisへ戻る。eventを価格session / ADVへ混ぜる変更も失敗する。
- positive treasury後のlater issued-only guardを外すと旧TrShを再控除する。明示0または新しいTrSh観測では再開する。
- `AvgSh` fallbackを戻すと4167型が期中平均をgross issuedへ保存する。単純等値だけのguardは正常1Qを止めるため失敗する。
- TA / EqARを独立carryへ戻すとsame-state PBR fixtureが変わる。near-zero公表精度、negative equity、新しいBPS fallbackを隣接固定する。
- universeのas-of filterを外すとfuture bar注入でmarket capとturnoverが変わる。
- forward event lookbackをentry価格の15日窓へ戻すと、FY期間内の分割前配当を75円として数える。正しくは支払basis換算後50円である。
- 同一periodの営業利益priorityを外すと、部分訂正後の営業利益100を捨て、純利益70へ置換する。

## 10. 採否、一次情報、限界

一次情報の主な照合先は次のとおりである。

- 6731: [JPX売買停止](https://www.jpx.co.jp/news/1030/20231214-01.html)、[100株→1株併合](https://www2.jpx.co.jp/disc/67310/140120231126594682.pdf)
- 9628: [2026-05-15決算資料](https://www2.jpx.co.jp/disc/96280/140120260514532304.pdf)
- 6184: [2026-03-12決算短信](https://www2.jpx.co.jp/disc/61840/140120260312580332.pdf)
- 7049: [自己株式処分資料](https://www2.jpx.co.jp/disc/70490/140120260414503805.pdf)
- 4889: [2026-05-13決算資料](https://www2.jpx.co.jp/disc/48890/140120260512527168.pdf)
- 9441: [自己株式消却完了](https://www.bellpark.co.jp/en/wp-content/uploads/sites/2/2025/07/release20250704_en.pdf)、[株式情報](https://www.bellpark.co.jp/ir/stock/info/)
- 4167: [株式情報](https://www.kokopelli-inc.com/ir/stock/)

既知の限界は次のとおりである。

- `TrShFY`のNULLは0株と未報告を区別せず、4889を含む保守的false positiveが残る。
- 古い明示0の`TrShFY`をlater issuedへcarryする反対側は、currentの誤りを一次情報で確認できず変更していない。
- 同日複数documentと訂正版identityはschema 23が保持しない。
- NaN current forecastから`Nx*` forecastへのcross-period fallbackはpersisted raw provenanceがない。
- period-wide snapshot / TTM / forecast recoveryは固定3y / 5y authorityを通らず不採用である。
- S8は全fieldのsemantic mutation対応表まで証明していない。

この監査は、観測可能なsourceとproduction functionに対するbasis整合を証明する。欠けたraw provenanceを推測で復元せず、値を安全に決められない場合はreason付きでavailabilityを閉じる。current top 5不変は修正不要の根拠ではなく、履歴return改善も採用理由にしていない。
