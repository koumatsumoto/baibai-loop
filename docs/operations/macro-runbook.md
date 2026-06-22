---
title: "Macro analysis runbook"
summary: "How the macro analysis capability works: pull stats data, write a macro-analysis record that names trade levers, and feed select / portfolio policy. The mechanical screen stays macro-blind."
doc_type: operations
status: active
last_reviewed: 2026-06-22
related_docs:
  - "../concepts.md"
  - "../reference/data-sources.md"
---

# Macro analysis runbook

マクロ環境分析は**独立した capability**（データ取得層 ＋ リサーチ実践）であり、formal なループにはしない。改善（調査方法・データソース確認手順のナレッジ）は使いながら都度蓄積する。狙いは「環境を読んでトレード判断に効く環境読みを供給する」こと。抱えるのは性質の違う 3 種：**① データ（事実）／ ② 分析成果（lever 必須の判断）／ ③ ナレッジ（メタ知識）**。

## ① データ：stats series を引く

統計 series は `baibai-loop-stats`（Provider モジュール設計、`src/baibai_loop/stats/`）で再現可能・provenance 付きに取得・キャッシュする。

```bash
uv run baibai-loop-stats list --domain rates       # 登録 series を見る
uv run baibai-loop-stats search 失業率              # 名前/alias/domain で検索
uv run baibai-loop-stats get jp.nikkei225 --start 2026-05-20 --end 2026-06-22
uv run baibai-loop-stats get jp.policy_rate --latest
```

`get` は coverage cache を見て miss のときだけ provider を呼ぶ。同入力なら同出力（決定論）。

### データソース registry（③ ナレッジの中核：どこを正に、どう確かめるか）

| Provider | 取得 | 担当ドメイン | 確認手順・既知の caveat |
| --- | --- | --- | --- |
| `fred_csv` | 無認証 CSV | 米マクロ・金利・FX・原油/金・VIX・クレジット OAS・BTC ＋ JP ミラー | 系列 ID を `fredgraph.csv?id=<ID>` の header 列で実 fetch 確認。**JP 系列は OECD 由来で月次/lag・廃止がある**（例: `JPNCPIALLMINMEI`・`CPALTT01JPM659N` は 2021 で停止）。日次が要る JP は別 provider |
| `frb_h15` | 無認証 CSV（H.15 package） | 米国債金利・スプレッド | 1 package を `FetchContext` で series 横断に 1 回 DL（bulk dedup） |
| `ecb_fx` | 無認証 ZIP | JPY クロス（USD/EUR/AUD） | ZIP 1 ファイルを横断共有。JPY と基軸通貨の比で算出 |
| `estat` | API（`ESTAT_APP_ID` 必須） | JP 公式マクロ（CPI・鉱工業生産・小売 等） | appId を env/.env に設定。`statsDataId` は e-Stat で確認。JP CPI の一次ソースはここ |
| `jquants_flows` | 認証（J-Quants refresh token） | JP 市場内部（海外投資家フロー 等） | screening と同じ credential を共用。週次 trades_spec |
| `boj` | 無認証 CSV | BOJ 時系列（マネタリーベース・短観 等） | データコードと CSV URL を BOJ stat-search で確認。文字コードは UTF-8/cp932 fallback |
| `manual` | ローカル file | 倒産件数（東商リサーチ）・PMI（au Jibun/S&P） | clean な無料 API が無い。`providers/manual_data.yaml` に手動更新し、値は必ず一次ソースで検証してから使う |

新ソース追加 = provider モジュールを 1 つ足して `series.yaml` に series を登録するだけ（`src/baibai_loop/stats/providers/` に 1 ファイル）。1 series_id = 1 provider を厳守する。

## ② 分析成果：macro-analysis record を書く

1 つの問いに対し、dated・sourced な分析を `records/02-macro-analysis/<YYYY>/<MM>/macro-analysis-<YYYY-MM-DD>-<slug>.yaml` に残す。schema は `records/_schemas/macro-analysis.json`、検証は：

```bash
uv run baibai-loop-validate --target macro-analysis
```

必須要件（schema/validator が強制）：

- **data_inputs**（≥1）：分析を ① stats series に grounding する。`series_id` は stats registry に存在すること（未登録は warning）。
- **scenarios**（≥1）・**risks**（≥1）・**forward_view**：深さ。
- **risk_posture**：`stance`（risk_on/neutral/risk_off）＋ rationale。
- **trade_levers**（≥1・lever 必須）：各分析は「トレード上の lever」を名指す。`lever ∈ {sector_tilt, risk_posture, theme, timing, universe_attention}`・`target`・`direction`・`rationale`。lever を名指せない問いは対象外（汎用リサーチ助手化の歯止め）。

スコープは広く（世界経済・政策・金利・FX・商品・INDEX・crypto・セクター・地政学）、ただし上記 lever 規律で接地する。

## ③ ナレッジ：都度洗練する

調査方法のコツ・source 確認手順・lessons は、リサーチを重ねるたびに上の registry と本 runbook に追記する（playbook 化は反復する問いタイプが現れてから）。formal な retro / calibration 機構は持たない。

## 接続：判断層にだけ効かせる（screen は macro-blind）

マクロ読みは機械スクリーニング `run` には接続しない（`run` は fundamentals の決定的 fact-engine のまま）。効くのは判断層だけ：

- **select**：`macro_analysis.sector_lever(analysis, sector)` が候補セクターの sector_tilt lever を返す。既存の macro-context sector-tilt lens と整合し、avoid セクターの割安株も surface はするが減点・flag（hard gate にしない）。
- **portfolio policy**：`macro_analysis.risk_posture_sizing_factor(posture)` が risk posture を starter サイズの倍率にする（risk_on=1.0 / neutral=0.75 / risk_off=0.5）。最高値追いをフルサイズで追わせない。
- **research**：forward_view・themes・hazards を個別 thesis の背景 context に使う。

## 誠実性（honesty firewall）

マクロは N≈1 で、screening のような横断 N の forward 統計検証ができない。本 capability は **edge 数値・統計的有意・自動売買 score を出さない**。得るのは再現性と判断の grounding であって統計的厳密さではない。マクロは判断であって測られた driver ではない。
