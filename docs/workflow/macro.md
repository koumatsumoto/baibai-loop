---
title: "Workflow — macro analysis"
summary: "マクロ環境分析：指標 series を引き、姿勢（ディフェンシブ / リスクオン）とセクター・AI 前提を読む環境読みを macro-context record に残す。単一ループの入口。"
doc_type: workflow
status: active
last_reviewed: 2026-07-01
---

# Workflow — マクロ環境分析

単一ループ（[`../doctrine.md`](../doctrine.md) §2）の入口。マクロ環境分析は **独立した capability**（データ取得層 ＋ リサーチ実践）であり、formal なループにはしない。狙いは「環境を読んで、**どのリスク姿勢でどのセクターに向かうか** を判断層へ供給する」こと。改善（調査方法・データソース確認手順のナレッジ）は使いながら都度蓄積する。

抱えるのは性質の違う 3 種：**① データ（事実）／ ② 環境読み（macro-context record）／ ③ ナレッジ（メタ知識）**。macro は N≈1 の判断であり、edge 数値・統計的有意・自動 sizing 倍率は出さない（§誠実性）。

## ① データ：indicator series を引く

指標 series は `baibai-loop-macro`（`src/baibai_loop/macro/indicators/`）で再現可能・provenance 付きに取得・キャッシュする。

```bash
uv run baibai-loop-macro list --category rates       # 登録 series を見る
uv run baibai-loop-macro search 失業率              # 名前/alias/category で検索
uv run baibai-loop-macro get jp.nikkei225 --start 2026-05-20 --end 2026-06-22
uv run baibai-loop-macro get jp.policy_rate --latest
```

`get` は coverage cache を見て miss のときだけ provider を呼ぶ。同入力なら同出力（決定論）。

### データソース registry

| Provider | 取得 | 担当ドメイン | 確認手順・既知の caveat |
| --- | --- | --- | --- |
| `fred_csv` | 無認証 CSV | 米マクロ・金利・実質金利/期待インフレ・FX・原油・VIX・クレジット OAS・BTC・流動性・NFCI ＋ JP ミラー | 系列 ID を `fredgraph.csv?id=<ID>` の header で実 fetch 確認。**廃止系列あり**（JP OECD CPI は 2021 停止、金 LBMA は 2025/5 停止）。金・SOX は `yahoo`。 |
| `frb_h15` | 無認証 CSV | 米国債金利・スプレッド | 1 package を series 横断に 1 回 DL |
| `ecb_fx` | 無認証 ZIP | JPY クロス（USD/EUR/AUD） | JPY と基軸通貨の比で算出 |
| `estat` | API（`ESTAT_APP_ID`） | JP 公式マクロ（CPI・鉱工業生産 等） | JP CPI の一次ソース。`statsDataId` は e-Stat で確認 |
| `jquants_flows` | 認証（`JQUANTS_API_KEY`） | JP 市場内部（海外投資家フロー） | screening と同じ credential |
| `boj` | 無認証 xlsx | BOJ 長期時系列（マネタリーベース 等） | `mblong.xlsx` を openpyxl で読む |
| `manual` | ローカル file | 倒産件数・PMI | clean な無料 API が無い。`providers/manual_data.yaml` に手動更新し一次ソースで検証 |
| `yahoo` | 無認証 JSON | 金/銀/銅先物・MOVE・Russell2000・SOX 等 | **ブラウザ UA 必須**（default は 429）。`provider_series_id` は Yahoo シンボル |
| `multpl` | 無認証 HTML | S&P500 バリュエーション（CAPE・GAAP PER・益回り） | HTML 構造変更で壊れる脆さ。追加時は `--latest` で live 確認 |

新ソース追加＝provider モジュールを 1 つ足して `series.yaml` に series を登録する（`providers/` に 1 ファイル）。1 series_id = 1 provider を厳守する。

### 運用テスト（series / provider を変更したら必ず回す）

データ層は forward 計測でなく **運用テスト** で品質を担保する。0 件 fail で通す：(1) 全 series スイープ（`list | get --latest`）で error / stale を 0、(2) 桁・単位 sanity、(3) provider ストレス（rate-limit 系を 1 プロセスで refresh し 429 が出ないか）、(4) 派生計算の単位整合（net liquidity = FRB総資産 − RRP − TGA、単位換算を明示）、(5) alias 解決、(6) 決定論、(7) `uv run pytest` と `uv run baibai-loop-validation`。

## ② 環境読み：macro-context record を書く

市場局面について dated・sourced な環境読みを `records/01-macro-context/<YYYY>/<MM>/macro-context-<YYYY-MM-DD>-<slug>.yaml` に残す。schema は `records/_schemas/macro-context.json`（contract-of-record）、検証は `uv run baibai-loop-validation --target macro-context`。

主な field：

- `context_id` / `as_of` / `valid_until` / `published_at`
- `inputs.articles`：外部記事の source / title / url / used_for（記事本文や監査ログは保存しない）
- `inputs.indicator_series`：`baibai-loop-macro` で確認した series と window
- `sector_tilts.items`：`sector_33` exact match で使う姿勢 tilt（`key` / `stance` / `strength` / `confidence`）
- `research_questions` / `refresh_triggers` / `changes_since_previous`

**record は分析レイヤーであり、手順（プロセス指示）を書かない**。「次回からこう調べる」等のプロセスは本 doc（workflow）に置く。record には screening / research の前提として使う環境読みと source metadata だけを残す。

**分析の独立性**：環境読みは、過去の客観的事実（価格・指標・イベント）は前提にしてよいが、過去の macro-context record の分析・結論（前回の sector tilt や相場観）は前提にしない。建玉（position）は分析に持ち込まない。一次情報と指標から解釈をゼロベースで組み立てる。過去 context との連続性は結論確定後に `changes_since_previous` へ事後接続する。

**更新トリガー**：macro-context は定期生成せず、**screening 前**（select が鮮度ある context を hard precondition にする）・**主要イベント後**（FOMC/BOJ/ECB/米CPI・PCE・NFP/地政学ショック）・**前回 `refresh_triggers` の発火** で必要時に更新する（基準 cadence は `valid_until = as_of + 7日` の実質週次）。

## ③ ナレッジ：8 分析レンズ

個別 series は単体でなく、以下のレンズに束ねて環境読みに使う（1 枚のパネルで横断的に読む）。操作手順・公開前の敵対的 self-check ゲートは skill [`macro-analysis`](../../.claude/skills/macro-analysis/SKILL.md) に集約する。

1. **グローバル流動性**：net liquidity ≈ `us.fed_assets` − `us.reverse_repo` − `us.tga`（単位換算注意）。`us.m2` 前年比はリスク資産に約 10 週先行。
2. **実質金利・store-of-value**：`us.real_10y` + `us.breakeven_10y` + `usd_index.broad` + `gold`。名目 = 実質 + 期待インフレに分解。
3. **金融環境の合成**：`us.nfci` を `vix`・`us.move`・クレジット OAS と突き合わせ、slow-burn（広範化前の局所ストレス）を読む。
4. **リスク選好の温度計**：`btc_usd` + `vix` + `credit.us_hy_oas`/`credit.us_ccc_oas` + `us.nfci`。BTC は先行温度計になりやすい（単独 driver にはしない）。
5. **景気サイクル・breadth**：`us.initial_claims` + `us.industrial_production` + `copper` + `us.russell2000` + `us.10y_3m_spread`。`us.sox` は AI/半導体サイクルと日本半導体株の先行ゲージ。
6. **バリュエーション・ERP**：`us.sp500_earnings_yield` − `us.10y` ＝ ERP。益回り < 名目金利（ERP≤0）は警戒域。`us.sp500_cape` で長期割高度。
7. **グローバル中銀の同期**：`us.fed_funds.upper` + `jp.policy_rate` + `ecb.policy_rate`。1 国でなく同期を読む。
8. **エネルギー・地政学**：`wti`/`brent` + `gold`。日本はエネルギー輸入依存が高く（中東 ~95%・ホルムズ ~74%）原油 spike が通貨・スタグフレーションに直結するため `usd_jpy` と併読。

## 姿勢とセクター、AI 中心

環境読みは **リスク姿勢とセクター配分** に落とす（[`../doctrine.md`](../doctrine.md) 柱 2）。

- **リスクを取るべきでない局面**（高値圏・ERP≤0・信用二極化・流動性逆風・地政学テール）：**ディフェンシブ** へ寄せ、余力を厚く保つ（[`../portfolio-management.md`](../portfolio-management.md)）。
- **リスクを取るべき局面**（過度な悲観・割安拡大・流動性追い風）：**追い風セクター** へ配分し、暴落では余力を投下する。
- **AI は中心セクター**。AI 産業革命を前提に長期の産業成長とマクロを組む。ただし AI 期待を単独の採用/sizing 根拠にはしない。

## 接続：判断層にだけ効かせる（screen は macro-blind）

マクロ読みは機械スクリーニング `run` には接続しない（`run` は fundamentals の決定的 fact-engine のまま）。効くのは判断層だけ：

- **select**（[`./screening.md`](./screening.md)）：`sector_tilts` が候補セクターの追い風/向かい風 lens として効く。avoid セクターの割安株も surface はするが減点・flag（hard gate にしない）。
- **portfolio management / research**：姿勢・themes・hazards を投下 timing・sizing 判断や個別 thesis の背景 context に使う。マクロを numeric driver にしない。

## 誠実性（honesty firewall）

マクロは N≈1 で、screening のような横断 N の forward 統計検証ができない。本 capability は edge 数値・統計的有意・自動売買 score を出さない。得るのは再現性と判断の grounding であって統計的厳密さではない。

## 参考

- [`../doctrine.md`](../doctrine.md)：思想・柱 2（macro が姿勢を決める）
- [`../portfolio-management.md`](../portfolio-management.md)：投下 timing・余力
- [`./screening.md`](./screening.md)：sector_tilts を使う select
- [`../reference/data-sources.md`](../reference/data-sources.md)：データソース Tier
