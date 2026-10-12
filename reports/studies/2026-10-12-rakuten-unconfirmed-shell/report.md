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
