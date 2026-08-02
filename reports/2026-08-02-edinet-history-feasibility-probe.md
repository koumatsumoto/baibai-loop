---
title: "EDINET歴史point-in-time抽出のfeasibility probe"
summary: "2021-05-31断面のEDINET歴史CSVは取得可能だが、現行の単一書類選択ではhidden-assets入力coverageが事前基準を下回るためbackfillを採用しない。"
doc_type: report
status: active
date: 2026-08-02
---

# EDINET歴史point-in-time抽出のfeasibility probe

価値tier: T2 — hidden-assets axisの歴史入力が現行抽出方法で成立するかを実測し、成立しないbackfillへの投資を止める。

## 結論

**事前登録した判定は`abandon`。** EDINETの歴史documents listと`type=5` CSVは取得でき、rate limitやwall timeも障害ではなかった。一方、現行の「as-of以前の最新1書類を銘柄ごとに選ぶ」方法では、`debt`、`InvestmentSecurities`、3項目同時入力のcoverageが事前基準を下回った。

hidden-assetsの歴史backfillは実装しない。`asset_backed_ratio`はforwardで蓄積する。年次書類の値を四半期断面へ持ち越す、または複数書類を合成する案は、値の有効期間と訂正のpoint-in-time規則を新たに定義する別methodであり、このprobeの失敗後に採否基準を動かして採用しない。

## 1. 事前登録した実行契約

- historical as-of: `2021-05-31`
- document-list window: `2019-12-08..2021-05-31`（541日）
- production liquidity population: 1,414銘柄
- population source: `data/screening/calibration/panel-2021-05-31.csv`の`in_population=true`
- document selection: 現行`select_document_candidates`（書類eventを畳み、tickerごとの最新usable CSV filingを選択）
- sample: population内候補を`sha256("2021-05-31:<ticker>:<doc_id>")`昇順に並べた先頭50件
- parser: 現行`parse_csv_zip_metric_record`
- external cache / SQLite: `/tmp`の隔離領域。canonical `data/screening/market.sqlite`は変更しない

採否条件は外部呼び出し前に[#760のコメント](https://github.com/koumatsumoto/baibai-loop/issues/760#issuecomment-5157400616)へ固定した。主な閾値はCSV取得90%以上、cash/debt各80%以上、`InvestmentSecurities`と3項目同時入力各60%以上、全件投影3時間以内である。1条件でも外せば`abandon`とした。

## 2. 一次APIと実測値

一次sourceはEDINET API v2のdocuments list（`https://api.edinet-fsa.go.jp/api/v2/documents.json`）とdocument download（`https://api.edinet-fsa.go.jp/api/v2/documents/<docID>`、`type=5`）である。subscription keyは実行環境から読み、URL・出力・reportへ保存していない。

| 項目 | 実測 | 事前基準 | 判定 |
| --- | ---: | ---: | --- |
| documents list成功 | 541 / 541日（100%） | 100% | pass |
| documents | 134,274件 | — | 観測値 |
| selected filing（全銘柄） | 3,892件 | — | 観測値 |
| population selected coverage | 1,414 / 1,414（100%） | 90%以上 | pass |
| sampled `type=5` ZIP取得 | 50 / 50（100%） | 90%以上 | pass |
| parser hard failure | 0 / 50（0%） | 10%以下 | pass |
| cash non-null | 43 / 50（86%） | 80%以上 | pass |
| debt non-null | 38 / 50（76%） | 80%以上 | **fail** |
| `InvestmentSecurities` non-null | 24 / 50（48%） | 60%以上 | **fail** |
| 3項目同時non-null | 22 / 50（44%） | 60%以上 | **fail** |

標本のdocument typeは有価証券報告書`120`が1件、四半期報告書`140`が49件だった。取得不能やparse errorではなく、最新書類を選ぶと四半期報告書に偏り、hidden-assetsのBS入力が揃わないことが失敗の中心である。欠損を0へ補完していない。

## 3. API量とwall time

| 工程 | 実測 |
| --- | ---: |
| 541日documents list | 24.84秒 |
| 50書類download + parse | 2.87秒 |
| 1書類平均 | 0.0574秒 |
| 3,892書類の直列投影 | 248.18秒（約4分8秒） |

429、retry exhaustion、5xx、不正payloadは発生しなかった。したがってissueで懸念したAPI量・rate limit・wall timeは、この1断面では採用を妨げない。失敗理由は入力coverageである。

## 4. 判断境界

- EDINETの歴史CSV提供経路そのものは成立している。
- 現行の単一最新書類selector/parserをそのまま歴史backfillへ使う経路は、hidden-assets入力coverageを満たさない。
- 年次書類だけを別に選ぶ、年次BSを後続四半期へcarry-forwardする、複数書類からmetricを合成する方法は、current extractionと異なるsource identity・訂正event・有効期間を持つ。probe通過前のbackfill tooling実装というスコープ外へ踏み込むため採らない。
- 1 as-of・50書類のfeasibility標本であり、効果量や将来returnの検定ではない。閾値の再調整、別標本への差し替え、軸の採否判断には使わない。

## 5. 次の扱い

`reports/2026-08-02-hidden-assets.md`の結論どおり、`asset_backed_ratio`はforward蓄積を待つ。歴史backfillのfollow-up issueは作らない。将来、年次BS carry-forwardを独立methodとして検討する場合は、point-in-time有効期間・訂正書・上場廃止を含む別の事前登録から始める。
