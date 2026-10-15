---
title: "楽天未収録2,265件の保存応答監査"
summary: "共通no-estimatesページの全件監査と、公式関連経路の少数対照。"
doc_type: study
status: historical
---

# 楽天未収録2,265件の保存応答監査

2026-10-12、[Issue #1407](https://github.com/koumatsumoto/baibai-loop/issues/1407#issuecomment-6116231555)の追加調査。
検証対象codeはmain `f748f8b11543c87c65c3dcc0ee809714456b5d10`。
全件取得は追加せず、保存済み応答の再解析と少数の通常HTTPを使った。

## 結果

| 保存済み旧楽天失敗応答 | 件数 | 現parserでの再検証 |
| --- | ---: | --- |
| 銘柄に結び付くchartと現在値の欠測があるページ | 437 | 既回復の正常nullと一致 |
| 銘柄を示さないno-estimates共通ページ | 2,265 | `no_estimates_identity_unconfirmed`を維持 |
| 合計 | 2,702 | 保存hash全件一致 |

未収録2,265件はすべて同一の4,821 bytesで、SHA256は
`5905324ef90f1256b467031629b7a9057b6cb62c0330026ae4362bc4abb080c4`。
全件に明示的なno-estimates段落が1件あり、tableとchart widgetは0件だった。
chart用script assetは存在するが、銘柄を指定するconstructorはない。
cache keyの数字に銘柄文字列が偶然含まれる例もあり、文字列一致は同一性の根拠にならない。
この保存集合に、数値を回復できる表・identityの取り逃しは0件だった。

新全件runの74 checkpointでは楽天がsuccess 990、normal_null 437、failure 2,265。
旧保存応答の未収録ticker集合と新checkpointの同理由のfailure集合が完全一致した。
437件の再解析結果は既offline観測の全metadataと元受領時刻に一致した。
旧pair・offline pair・新版handoffの14,843ファイルは監査前後のSHA256がすべて不変だった。
入力hash manifestのdigestは
`3178c6ea7f1c40c32008715ffd44bc8b83f784c38003d9a93059e4483265679b`。
新runは失敗HTMLを保存していないため、このbody構造監査は旧保存応答についての結果である。

## 少数の通常HTTP対照

2026-10-12 01:37:51–01:38:03 UTCに、IFIS `report_est`を6回検証した。
認証情報を使わず、直列、前応答完了から2秒以上、通常proxy/TLS、redirectなしとした。

| `bcode` | HTTP | 構造 |
| --- | ---: | --- |
| `1301.T` / `1301` | 各200 | 保存2,265件と同一bytes |
| `130A.T` / `130A` | 各200 | 保存2,265件と同一bytes |
| 無効code `INVALID.T` | 200 | 保存2,265件と同一bytes |
| 正対照 `7203.T` | 200 | table 4件、chart constructor 1件、no-estimates段落なし |

`.T`を除去しても変化しなかった。無効codeにも同じbodyが返るため、このbodyだけでは
銘柄別の真のabsenceを確定できない。先行する独立診断1HTTPはHTML metaのEUC-JPによる
復号に失敗し未解析だった。上表は通常collectorと同じContent-Type優先のUTF-8で検証した6HTTPである。

追加指示により01:42:38–01:42:57 UTCに、公式forecastが示す関連経路を8HTTP確認した。
条件は上と同じであり、最後のexplicit 403で全追加HTTPを停止した。

| 公開経路 | 確認できたこと | rating回復の範囲 |
| --- | --- | --- |
| 公式`hp2/hp_<RIC>.html`、1301・130A | 会社名とcodeを持つ見出し | rating・consensus欄なし |
| 公式`profile.html?ric=1301.T` | 会社名・codeとTRKD iframe指定 | iframe本体は未取得 |
| 公式`performance.html?ric=1301.T` → IFIS `sa=report` | 1301に結び付く4種の財務chartと表 | 会社実績2行・会社予想1行、analyst consensus行0、rating欄なし |
| 公式`forecast.html?ric=1301.T` → IFIS `sa=report_est` | 正式なiframe要求先 | 応答は同じ未確認shell |
| 正対照で得た公開graph URL templateを1301へ適用 | `img-sec.ifis.co.jp/graph/chart_op_cons/1301.json`が403 | そこで停止、認証追加・経路変更なし |

graph URLは正対照のconstructorから得たtemplateであり、1301のshellに掲載されていたlinkではない。
公式関連画面では銘柄の存在と会社予想を確認できたが、会社予想はratingへ代用できない。
outerの共通ログインnavigationは公開ページにも含まれ、今回の成功したHTTPに認証は不要だった。
403の原因・必要な認証、および未取得のTRKD iframeで評価が得られるかは未確認である。

## 採用する対応と限界

現行の未収録判定を維持する。新しい値の回復は0件であり、全2,265銘柄に予測やratingが
存在しないと確定した結果でもない。別応答で銘柄が認識されたことは、同一性を持たない
rating応答そのものの証明を補わない。観測の扱いは[data sources](../../../docs/reference/data-sources.md#retail-dynamics)に統合した。

合成fixtureで共通shell・script asset・偶然のticker文字列・付随するrequest URLを検証し、
正しいofficial request contextでも観測を作らず2HTTPの確定失敗にする回帰を補強する。
Rakuten parser version、取得policy、collector binding、schema、保存済みplanと受領時刻は変更しない。
将来、ratingを銘柄へ結び付ける公開応答が確認された場合に、その根拠から取得方式を検討する。

raw応答・値・pair別記録は私有検証directoryへ保持し、Gitには含めていない。
本番/R2の書込・削除、追加全件取得、deploy、Discord送信は行っていない。

## Issue #1429の運用判断と読取連携

同日の[Issue #1429](https://github.com/koumatsumoto/baibai-loop/issues/1429)では、main
`56aa8d43918eaab70badfe1129e014c2ae837ae6`を基準に上の証拠と現行実装を点検した。
上の全件body監査・集合照合は繰り返していない。3,692件は試行済みであり、利用できる
rating数値は990件、銘柄を確認できた正常nullは437件、残る2,265件は真の不存在や
対象外を確認できない欠測として扱う。追加回収可能と確認できた件数は0件である。
正常nullも、その後を含む評価の永続的な不存在を証明するものではない。

[公式forecast outer](https://www.rakuten-sec.co.jp/web/market/search/forecast.html?ric=1301.T)の
公開読取では提供対象条件の説明を確認できず、公式domainの説明検索も該当資料を特定できなかった。
公式説明を調査し尽くしたとは扱わず、2,265件のrating不存在・提供対象外や必要な認証を
確定する根拠は得られていない。公式outerの銘柄認識は
iframeの同一性の代替にしない。graph経路には通信せず、header・proxy・TLS・認証を変更していない。

現行collectorの少量対照を2026-10-12 14:56:27–14:56:49 UTCに、
7203・1379・1301の3銘柄、計6 HTTPで通常公開経路から行った。
全応答200、直列、応答完了から次要求まで2秒以上、redirect・retryなし。
7203はrating値を持つ観測1件、1379は銘柄を確認できた正常null1件、1301は
`no_estimates_identity_unconfirmed`で観測0件だった。未確認shellのbodyは上の監査digestに一致。
これは新しい全件coverageの測定や追加値回復ではなく、現行実装と実応答の少量対照である。
値・raw本文・私有保存先は公開しない。

取得parser・policy・checkpoint binding・観測schemaは変更しない。既存の日次固定planでは
確定failureを再開時に再取得せず、翌日以降の通常日次planは現行対象を維持する。
未知欠測を恒久的な除外へ変える根拠もない。提供元の拒否は現行停止条件に従う。
不存在や追加取得条件の確定を求める場合は、提供元から銘柄単位の説明を得る必要があり、
新しいログイン・credentials・課金が必要かは未確認のまま親へ引き継ぐ。

最小の改善は、保存観測がない楽天系列が分析結果から消えていた読取経路の修正である。
`same-series-v3`は楽天の媒体名を持つ`not_observed` / `no_observation`を返し、
観測行・受領時刻・原評価元・独立票は作らない。正常nullは元の観測行と`missing` /
`latest_missing`を保持する。保存観測のない理由は読取storeだけでは確定できないため、
Researchへcheckpointの失敗理由を推測して渡さない。他社目標値・会社予想でratingを補わない。
画面は未知欠測・正常null・最新保存値を分け、保存値を現在値と保証しない。

合成の値0・正常null・未確認shellをparser→checkpoint保存/読戻し→stage→隔離DB・再投入→
API・serving export・Research入力まで照合し、正常nullの元受領時刻とcutoffを保つ。
実応答の元encoding・元受領時刻での再解析はstage全列に一致し、隔離DBへ2観測を投入、
再投入0件、cutoff直前の除外と読取前後のDB不変を確認した。実応答由来の3系列はqueryと
Research入力で一致し、typed2銘柄はAPIとserving exportでも一致した。隔離copyには1301の
master・他領域観測がないため、APIは404であり、未観測だけの1301をexport対象へ追加してはいない。
他媒体値がある銘柄、および既存screeningにある外部観測なしの銘柄について、未知楽天系列を
API・exportへ保持することは合成連携で検証した。未観測の表示を実観測ありという銘柄認識へ
使わないよう、Webの存在判定も観測行の有無へ修正した。
本番反映・全件数値取得の受入とは区別する。
この報告提出時点では、最終方針とPRの採否、merge・配信更新、親#1414の運用受入は親レビュー待ちだった。現在の進捗はリンク先 Issue / PR の状態を参照する。

提出前の完全local gateはPython 4,040 passed / 8 skipped、coverage 86.21%、
frontend 258件、Worker 88件、build・型検査・Ruff・mypy・30 import契約・drift・
Bandit・lock由来runtime/dev/build-system依存監査・npm監査・Worker dry-runが成功した。
8 skipは既存の専用R2受入credentials未設定であり、新しい認証や本番書込は行っていない。
