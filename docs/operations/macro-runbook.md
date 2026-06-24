---
title: "Macro analysis runbook"
summary: "How the macro analysis capability works: pull indicator series data and write a macro-context record (an environment read) whose sector tilts feed select / portfolio policy. The mechanical screen stays macro-blind."
doc_type: operations
status: active
last_reviewed: 2026-06-23
related_docs:
  - "../concepts.md"
  - "../reference/data-sources.md"
---

# Macro analysis runbook

マクロ環境分析は**独立した capability**（データ取得層 ＋ リサーチ実践）であり、formal なループにはしない。改善（調査方法・データソース確認手順のナレッジ）は使いながら都度蓄積する。狙いは「環境を読んでトレード判断に効く環境読みを供給する」こと。抱えるのは性質の違う 3 種：**① データ（事実）／ ② 環境読み（macro_context record）／ ③ ナレッジ（メタ知識）**。

## ① データ：indicator series を引く

指標 series は `baibai-loop-macro`（Provider モジュール設計、`src/baibai_loop/macro/indicators/`）で再現可能・provenance 付きに取得・キャッシュする。

```bash
uv run baibai-loop-macro list --category rates       # 登録 series を見る
uv run baibai-loop-macro search 失業率              # 名前/alias/category で検索
uv run baibai-loop-macro get jp.nikkei225 --start 2026-05-20 --end 2026-06-22
uv run baibai-loop-macro get jp.policy_rate --latest
```

`get` は coverage cache を見て miss のときだけ provider を呼ぶ。同入力なら同出力（決定論）。

### データソース registry（③ ナレッジの中核：どこを正に、どう確かめるか）

| Provider | 取得 | 担当ドメイン | 確認手順・既知の caveat |
| --- | --- | --- | --- |
| `fred_csv` | 無認証 CSV | 米マクロ・金利・FX・原油/金・VIX・クレジット OAS・BTC ＋ JP ミラー | 系列 ID を `fredgraph.csv?id=<ID>` の header 列で実 fetch 確認。**JP 系列は OECD 由来で月次/lag・廃止がある**（例: `JPNCPIALLMINMEI`・`CPALTT01JPM659N` は 2021 で停止）。日次が要る JP は別 provider |
| `frb_h15` | 無認証 CSV（H.15 package） | 米国債金利・スプレッド | 1 package を `FetchContext` で series 横断に 1 回 DL（bulk dedup） |
| `ecb_fx` | 無認証 ZIP | JPY クロス（USD/EUR/AUD） | ZIP 1 ファイルを横断共有。JPY と基軸通貨の比で算出 |
| `estat` | API（`ESTAT_APP_ID` 必須） | JP 公式マクロ（CPI・鉱工業生産・小売 等） | appId を env/.env に設定。`statsDataId` は e-Stat で確認。JP CPI の一次ソースはここ |
| `jquants_flows` | 認証（J-Quants API キー `JQUANTS_API_KEY`） | JP 市場内部（海外投資家フロー 等） | screening と同じ credential を共用。週次 trades_spec |
| `boj` | 無認証 xlsx | BOJ 長期時系列（マネタリーベース 等） | 安定 URL の `mblong.xlsx`（平残シート）を openpyxl で読む。`provider_series_id` は値列番号（C 列=マネタリーベース=3） |
| `manual` | ローカル file | 倒産件数（東商リサーチ）・PMI（au Jibun/S&P） | clean な無料 API が無い。`providers/manual_data.yaml` に手動更新し、値は必ず一次ソースで検証してから使う |

新ソース追加 = provider モジュールを 1 つ足して `series.yaml` に series を登録するだけ（`src/baibai_loop/macro/indicators/providers/` に 1 ファイル）。1 series_id = 1 provider を厳守する。

## ② 環境読み：macro_context record を書く

市場局面について、dated・sourced な環境読みを `records/01-macro-context/<YYYY>/<MM>/...yaml` に残す。schema は `records/_schemas/macro-context.json`、検証は：

```bash
uv run baibai-loop-validation --target macro-context
```

環境読みは ① indicator series に grounding し、**sector tilt**（業種ごとの追い風/向かい風 ＝ `key`/`stance`/`strength`/`confidence`）と市場前提を記す。スコープは広く（世界経済・政策・金利・FX・商品・INDEX・crypto・セクター・地政学）。マクロは N≈1 の判断なので、**edge 数値・統計的有意・lever の機械適用（自動 sizing 倍率）は出さない**。環境読みは次の接続で判断層の背景としてのみ効く。

**分析の独立性**: 環境読みは、過去の客観的「事実」（価格・指標・イベント等のデータ）は前提にしてよいが、過去の macro_context record の「分析・結論」（前回の sector tilt や相場観）は前提にしない。建玉（position）は分析に持ち込まない。一次情報と指標から解釈をゼロベースで組み立てる。確証バイアス・アンカリングと、保有を正当化する motivated reasoning を避けるための規律であり、N≈1 で forward 検証が効かないマクロでは独立性が質の生命線になる。過去 context との連続性は `changes_since_previous` に、独立した結論が出た後で事後に接続する。

## ③ ナレッジ：都度洗練する

調査方法のコツ・source 確認手順・lessons は、リサーチを重ねるたびに上の registry と本 runbook に追記する（playbook 化は反復する問いタイプが現れてから）。formal な retro / calibration 機構は持たない。

## 接続：判断層にだけ効かせる（screen は macro-blind）

マクロ読みは機械スクリーニング `run` には接続しない（`run` は fundamentals の決定的 fact-engine のまま）。効くのは判断層だけ：

- **select**：`macro_context` の sector tilt が候補セクターの追い風/向かい風 lens として効く（`screening/selection/macro_fit`）。avoid セクターの割安株も surface はするが減点・flag（hard gate にしない）。
- **portfolio policy / research**：環境前提・themes・hazards を sizing 判断や個別 thesis の背景 context に使う。具体の sizing は portfolio policy が決め、マクロは背景に留める（マクロを numeric driver にしない）。

## 誠実性（honesty firewall）

マクロは N≈1 で、screening のような横断 N の forward 統計検証ができない。本 capability は **edge 数値・統計的有意・自動売買 score を出さない**。得るのは再現性と判断の grounding であって統計的厳密さではない。マクロは判断であって測られた driver ではない。
