---
title: "Workflow — macro analysis"
summary: "マクロ環境分析：指標データを引き、リスク姿勢（ディフェンシブ / リスクオン）とセクター・AI 前提を読んだ環境認識を macro-context record に残す。単一ループの入口。"
doc_type: workflow
status: active
last_reviewed: 2026-07-02
---

# Workflow — マクロ環境分析

単一ループ（[`../doctrine.md`](../doctrine.md) §2）の入口。マクロ環境分析は **独立した機能のまとまり**（データ取得層 + リサーチの実践）であり、形式化した独自ループにはしない。狙いは、環境を読んで「**どのリスク姿勢で、どのセクターに向かうか**」を判断層へ供給すること。改善（調査方法・データソース確認手順の知見）は、使いながらその都度蓄積する。

扱うものは性質の異なる 3 種：**① データ（事実）／ ② 環境認識（macro-context record）／ ③ 知見（調べ方のメタ知識）**。マクロは標本数がほぼ 1 の判断であり、優位性の数値・統計的有意性・自動の投入額倍率は出さない（§誠実性）。

## ① データ：indicator series を引く

指標データは `baibai-loop-macro`（`src/baibai_loop/macro/indicators/`）で、再現可能かつ出所（provenance）付きで取得・キャッシュする。

```bash
uv run baibai-loop-macro list --category rates       # 登録 series を見る
uv run baibai-loop-macro search 失業率              # 名前/alias/category で検索
uv run baibai-loop-macro get jp.nikkei225 --start 2026-05-20 --end 2026-06-22
uv run baibai-loop-macro get jp.policy_rate --latest
uv run baibai-loop-macro refresh us.10y --start 2026-06-20 --end 2026-07-02   # provider を強制再取得
```

`get` は取得済み範囲のキャッシュを確認し、不足があるときだけ provider を呼ぶ。同じ入力には同じ出力を返す（決定論）。**取得済みの窓の中では provider を呼び直さないため、`get --latest` は「キャッシュ上の最新」を返すことに注意**（`observed_at` が数営業日前で止まっていることがある）。環境認識を書く直前は、判断に使う主要 series を `refresh` で直近窓ごと再取得してから `get --latest` を読む。

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

データ層の品質は **運用テスト** で担保する。すべて失敗 0 件で通す：(1) 全 series スイープ（`list | get --latest`）で error / stale を 0、(2) 桁・単位の妥当性、(3) provider ストレス（rate-limit 系を 1 プロセスで refresh し 429 が出ないか）、(4) 派生計算の単位整合（net liquidity = FRB総資産 − RRP − TGA、単位換算を明示）、(5) alias 解決、(6) 決定論、(7) `uv run pytest` と `uv run baibai-loop-validation`。

## ② 環境認識：macro-context record を書く

市場局面についての、日付と出所の明確な環境認識を `records/01-macro-context/<YYYY>/<MM>/macro-context-<YYYY-MM-DD>-<slug>.yaml` に残す。schema は `records/_schemas/macro-context.json`（contract-of-record）、検証は `uv run baibai-loop-validation --target macro-context`。

主な field：

- `context_id` / `as_of` / `valid_until` / `published_at`
- `inputs.articles`：外部記事の source / title / url / used_for（記事本文や監査ログは保存しない）
- `inputs.indicator_series`：`baibai-loop-macro` で確認した series と window
- `sector_tilts.items`：`sector_33` exact match で使う姿勢 tilt（`key` / `stance` / `strength` / `confidence`）
- `research_questions` / `refresh_triggers` / `changes_since_previous`

**record は分析レイヤーであり、手順（作業の指示）を書かない**。「次回からこう調べる」といった手順の話は本 doc（workflow）に置く。record には、screening / research の前提として使う環境認識と出所のメタデータだけを残す。

**分析の独立性**：環境認識の前提にしてよいのは過去の客観的事実（価格・指標・イベント）だけで、過去の macro-context record にある分析・結論（前回の sector tilt や相場観）は前提にしない。保有中の建玉も分析に持ち込まない。一次情報と指標から、解釈を毎回ゼロベースで組み立てる。過去の context との連続性は、結論を確定させた後に `changes_since_previous` として事後的に接続する。

**更新のきっかけ**：macro-context は定期的には生成せず、**screening の前**（select は鮮度のある context を前提条件にする）・**主要イベントの後**（FOMC / 日銀会合 / ECB / 米 CPI・PCE・雇用統計 / 地政学ショック）・**前回書いた `refresh_triggers` の発火**、のいずれかで必要になったときに更新する（`valid_until = as_of + 7 日` とするため、実質は週次）。

## ③ ナレッジ：8 分析レンズ

個別の指標は単体で読まず、以下のレンズに束ねて環境認識に使う（1 枚のパネルとして横断的に読む）。操作手順と公開前の敵対的セルフチェックは skill [`macro-analysis`](../../.claude/skills/macro-analysis/SKILL.md) に集約する。

1. **グローバル流動性**：net liquidity ≈ `us.fed_assets` − `us.reverse_repo` − `us.tga`（単位換算注意）。`us.m2` 前年比はリスク資産に約 10 週先行。
2. **実質金利・store-of-value**：`us.real_10y` + `us.breakeven_10y` + `usd_index.broad` + `gold`。名目 = 実質 + 期待インフレに分解。
3. **金融環境の合成**：`us.nfci` を `vix`・`us.move`・クレジット OAS と突き合わせ、slow-burn（広範化前の局所ストレス）を読む。
4. **リスク選好の温度計**：`btc_usd` + `vix` + `credit.us_hy_oas`/`credit.us_ccc_oas` + `us.nfci`。BTC は先行温度計になりやすい（単独 driver にはしない）。
5. **景気サイクル・breadth**：`us.initial_claims` + `us.industrial_production` + `copper` + `us.russell2000` + `us.10y_3m_spread`。`us.sox` は AI/半導体サイクルと日本半導体株の先行ゲージ。
6. **バリュエーション・ERP**：`us.sp500_earnings_yield` − `us.10y` ＝ ERP。益回り < 名目金利（ERP≤0）は警戒域。`us.sp500_cape` で長期割高度。
7. **グローバル中銀の同期**：`us.fed_funds.upper` + `jp.policy_rate` + `ecb.policy_rate`。1 国でなく同期を読む。
8. **エネルギー・地政学**：`wti`/`brent` + `gold`。日本はエネルギー輸入依存が高く（中東 ~95%・ホルムズ ~74%）原油 spike が通貨・スタグフレーションに直結するため `usd_jpy` と併読。

## 姿勢とセクター、AI 中心

環境認識は **リスク姿勢とセクター配分** という結論に落とす（[`../doctrine.md`](../doctrine.md) 柱 2）。

- **リスクを取るべきでない局面**（高値圏・ERP≤0・信用二極化・流動性逆風・地政学テール）：**ディフェンシブ** へ寄せ、余力を厚く保つ（[`../portfolio-management.md`](../portfolio-management.md)）。
- **リスクを取るべき局面**（過度な悲観・割安拡大・流動性追い風）：**追い風セクター** へ配分し、暴落では余力を投下する。
- **AI は中心に据えるセクター**。AI による産業革命を前提に、長期の産業成長とマクロ観を組み立てる。ただし AI への期待は、それ単独では採用理由にも投入額の根拠にもしない。

## 接続：判断層にだけ効かせる（screen は macro-blind）

マクロの読みは機械スクリーニングの `run` には接続しない（`run` は財務事実だけを扱う決定論的なエンジンのまま）。効かせるのは判断層だけ：

- **select**（[`./screening.md`](./screening.md)）：`sector_tilts` が候補セクターの追い風 / 向かい風の参考情報として効く。回避としたセクターの割安株も一覧には現れ、注意情報が付くだけで機械的には落とさない。
- **portfolio management / research**：姿勢・テーマ・警戒事項を、資金を投じるタイミングや投入額の判断、個別 thesis の背景情報に使う。マクロを数値ドライバーにはしない。

## 誠実性（honesty firewall）

マクロは標本数がほぼ 1 であり、screening のように多数の銘柄を横断する統計検証ができない。この工程は優位性の数値・統計的有意性・自動売買スコアを出さない。ここで得られるのは再現性と、判断を事実に根付かせる基盤であって、統計的な厳密さではない。

## 参考

- [`../doctrine.md`](../doctrine.md)：思想・柱 2（macro が姿勢を決める）
- [`../portfolio-management.md`](../portfolio-management.md)：資金投下のタイミング・余力
- [`./screening.md`](./screening.md)：sector_tilts を使う select
- [`../reference/data-sources.md`](../reference/data-sources.md)：データソース Tier
