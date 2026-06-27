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
| `fred_csv` | 無認証 CSV | 米マクロ・金利・実質金利/期待インフレ・FX・原油・VIX・クレジット OAS・BTC・流動性(FRB資産/RRP/M2)・金融環境(NFCI) ＋ JP ミラー | 系列 ID を `fredgraph.csv?id=<ID>` の header 列で実 fetch 確認。**FRED 系列は廃止がある**（JP OECD 系列 `JPNCPIALLMINMEI`・`CPALTT01JPM659N` は 2021 停止、**金 LBMA `GOLDAMGBD228NLBM` は 2025/5 停止・FRED に代替の clean な金系列が不在**）。日次 JP・金スポットは非 FRED provider が要る |
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

### 汎用分析レンズ（指標の束ね方）

個別 series は単体でなく、以下のレンズに束ねて環境読みに使う。いずれも全リスク資産に効く汎用フレームで、流動性・実質金利に感応する資産（growth 株・新興・コモディティ・crypto）で特に鋭く出る。1 枚のパネルで横断的に読む（`baibai-loop-macro get` を束ねる）。操作手順・**公開前の敵対的 self-check ゲート**・スキル自身の改善ループは skill [`macro-analysis`](../../.claude/skills/macro-analysis/SKILL.md) に集約する。

1. **グローバル流動性**: net liquidity ≈ `us.fed_assets`(FRB総資産) − `us.reverse_repo`(ON RRP) − TGA。`us.m2` の前年比はリスク資産に約10週先行する経験則の基軸。QT/QE の量的スタンスと RRP の枯渇/再構築を合わせ「流動性のトレンドとエンジンの有無」を読む（TGA は未登録。必要時 FRED `WTREGEN` 等を ad hoc 併用）。
2. **実質金利・store-of-value**: `us.real_10y`(実質金利) + `us.breakeven_10y`(期待インフレ) + `usd_index.broad`(ドル)。名目 `us.10y` = 実質 + 期待インフレ に分解し「割引率上昇が実質金利由来か期待インフレ由来か」を見る。実質金利上昇＋強いドルは無利回り資産（金・BTC）と長期グロースの一様な向かい風。
3. **金融環境の合成**: `us.nfci`(0=平均, 正=引締/負=緩和)を `vix`・クレジット OAS と突き合わせる。NFCI がまだ緩和的なのに特定資産が荒れる局面は「広範化前の局所ストレス（slow-burn）」と読む。
4. **リスク選好の温度計**: `btc_usd` + `vix` + `credit.us_hy_oas`/`credit.us_ccc_oas` + `us.nfci` を 1 枚のパネルで見る。BTC は最も流動性・リスク選好に感応するため先行温度計になりやすい（ただし単独の numeric driver にはしない＝誠実性ファイアウォール）。

## 接続：判断層にだけ効かせる（screen は macro-blind）

マクロ読みは機械スクリーニング `run` には接続しない（`run` は fundamentals の決定的 fact-engine のまま）。効くのは判断層だけ：

- **select**：`macro_context` の sector tilt が候補セクターの追い風/向かい風 lens として効く（`screening/selection/macro_fit`）。avoid セクターの割安株も surface はするが減点・flag（hard gate にしない）。
- **portfolio policy / research**：環境前提・themes・hazards を sizing 判断や個別 thesis の背景 context に使う。具体の sizing は portfolio policy が決め、マクロは背景に留める（マクロを numeric driver にしない）。

## 誠実性（honesty firewall）

マクロは N≈1 で、screening のような横断 N の forward 統計検証ができない。本 capability は **edge 数値・統計的有意・自動売買 score を出さない**。得るのは再現性と判断の grounding であって統計的厳密さではない。マクロは判断であって測られた driver ではない。
