---
title: "Workflow — macro analysis"
summary: "マクロ環境分析：指標と一次情報から、個別5年期待値を変えるmaterial deltaと共通riskを必要時だけmacro-context recordに残す。"
doc_type: workflow
status: active
last_reviewed: 2026-07-11
---

# Workflow — マクロ環境分析

マクロ環境分析は **独立した機能のまとまり**（データ取得層 + リサーチの実践）であり、形式化した独自ループにはしない。狙いは、個別銘柄の5年期待値を変え得る外部経路と共通riskを判断層へ供給すること。sector順位、相場方向、買い時、投入額を決めない。

扱うものは性質の異なる 3 種：**① データ（事実）／ ② 環境認識（macro-context record）／ ③ 知見（調べ方のメタ知識）**。マクロは標本数がほぼ 1 の判断であり、優位性の数値・統計的有意性・自動の投入額倍率は出さない（§誠実性）。

## ① データ：indicator series を引く

指標データは `baibai-engine macro`（`src/baibai_engine/macro/indicators/`）で、再現可能かつ出所（provenance）付きで取得・キャッシュする。

```bash
uv run baibai-engine macro list --category rates       # 登録 series を見る
uv run baibai-engine macro search 失業率              # 名前/alias/category で検索
uv run baibai-engine macro get jp.nikkei225 --start 2026-05-20 --end 2026-06-22
uv run baibai-engine macro get jp.policy_rate --latest
uv run baibai-engine macro refresh us.10y --start 2026-06-20 --end 2026-07-02   # provider を強制再取得
```

`get` は取得済み範囲のキャッシュを確認し、不足があるときだけ provider を呼ぶ。同じ入力には同じ出力を返す（決定論）。`get --latest` は frequency 別の鮮度窓（daily は 1 暦日、weekly は 14 日、monthly は 70 日）内の cache があればそれを返し、古い場合は最新確認用の短い窓（daily は 14 日、weekly は 60 日、monthly は 370 日）を provider で再取得する。環境認識を書く直前は、判断に使う主要 series を `refresh` で直近窓ごと再取得してから `get --latest` を読む。

### データソース registry

| Provider | 取得 | 担当ドメイン | 確認手順・既知の caveat |
| --- | --- | --- | --- |
| `fred_csv` | 無認証 CSV | 米マクロ・実質金利/期待インフレ・FX・原油・VIX・クレジット OAS・BTC・流動性・NFCI・JP 失業率 | 系列 ID を `fredgraph.csv?id=<ID>` の header で実 fetch 確認。**廃止系列あり**（JP OECD CPI は 2021 停止、金 LBMA は 2025/5 停止）。金・SOX は `yahoo`。 |
| `frb_h15` | 無認証 CSV | 米国債金利・スプレッド | 1 package を series 横断に 1 回 DL |
| `ecb_fx` | 無認証 ZIP | JPY クロス（USD/EUR/AUD） | JPY と基軸通貨の比で算出 |
| `estat` | API（`ESTAT_APP_ID`） | JP 公式マクロ（CPI・鉱工業生産 等） | JP CPI の一次ソース。`statsDataId` は e-Stat で確認 |
| `jquants_flows` | 認証（`JQUANTS_API_KEY`） | JP 市場内部（海外投資家フロー） | screening と同じ credential |
| `boj` | 無認証 xlsx | BOJ 長期時系列（マネタリーベース 等） | `mblong.xlsx` を openpyxl で読む |
| `boj_mutan` | 無認証 HTML + xlsx | BOJ 無担保コール O/N 確報 | 年別 index から `mdYYYYMMDD.xlsx` を辿り、確報 workbook の平均値を読む。公表タイミングは BOJ の日次更新予定に従う |
| `mof_jgb` | 無認証 CSV | JP 国債金利（主要年限） | `jgbcm_all.csv` と当月 `jgbcm.csv` を CP932 で読み、和暦の基準日を ISO date に正規化する |
| `manual` | ローカル file | 倒産件数・PMI | clean な無料 API が無い。`providers/manual_data.yaml` に手動更新し一次ソースで検証 |
| `yahoo` | 無認証 JSON | 金/銀/銅先物・MOVE・Russell2000・SOX 等 | **ブラウザ UA 必須**（default は 429）。`provider_series_id` は Yahoo シンボル |
| `multpl` | 無認証 HTML | S&P500 バリュエーション（CAPE・GAAP PER・益回り） | HTML 構造変更で壊れる脆さ。追加時は `--latest` で live 確認 |

新ソース追加＝provider モジュールを 1 つ足して `series.yaml` に series を登録する（`providers/` に 1 ファイル）。1 series_id = 1 provider を厳守する。provider 取得は一時的な `IndicatorsProviderError` を 1 回 retry し、再失敗した場合は `provider_runs` に failed として記録する。

### 運用テスト（series / provider を変更したら必ず回す）

データ層の品質は **運用テスト** で担保する。すべて失敗 0 件で通す：(1) 全 series スイープ（`list | get --latest`）で error / stale を 0、(2) 桁・単位の妥当性、(3) provider ストレス（rate-limit 系を 1 プロセスで refresh し 429 が出ないか）、(4) 派生計算の単位整合（net liquidity = FRB総資産 − RRP − TGA、単位換算を明示）、(5) alias 解決、(6) 決定論、(7) `uv run pytest` とmacro model / config loaderのnegative test。

## ② 環境認識：macro-context revision を publish する

市場局面についての、日付と出所の明確な環境認識は application DB の immutable revision として残す。機械契約は `baibai_engine.macro.models.MacroContextDocument`、唯一の書き込み経路は `baibai-engine macro context publish` である。既存 head を読んで draft を作り、2件目以降は `--expected-head` にその ID を渡す。head が変わっていれば publish 全体が無変更で失敗する。

```bash
uv run baibai-engine macro context head
uv run baibai-engine macro context publish /tmp/macro-context-draft.yaml \
  --expected-head macro-context-2026-07-01-example
uv run baibai-engine macro context show --latest --asof 2026-07-19
```

主な field：

- `context_id` / `as_of` / `valid_until` / `published_at`
- `inputs.articles`：外部記事の一意な`input_id`、source / title / url / published_at / accessed_at / status / used_for（記事本文や監査ログは保存しない）
- `inputs.indicator_series`：一意な`input_id`、`baibai-engine macro`で確認したprovider / series / window / observation_as_of / status / used_for
- `material_deltas`：discount rate、demand、funding、common tailのどれが変わったか、方向・重要度・使い道・根拠input
- `sizing_cautions`：個別の投入額を決めないが、proposalで可視化する共通risk
- `research_questions` / `refresh_triggers` / `changes_since_previous`

**revision は分析レイヤーであり、手順（作業の指示）を書かない**。「次回からこう調べる」といった手順の話は本 doc（workflow）に置く。revision には、screening / research の前提として使う環境認識と出所のメタデータだけを残す。

**分析の独立性**：環境認識の前提にしてよいのは過去の客観的事実（価格・指標・イベント）だけで、過去のmacro-context revisionにある分析・結論は前提にしない。保有中の建玉も分析に持ち込まない。一次情報と指標から、解釈を毎回ゼロベースで組み立てる。過去のcontextとの連続性は、結論を確定させた後に`changes_since_previous`として事後的に接続する。

**更新のきっかけ**：macro-contextは定期的には生成せず、discount rate・需要・資金調達・共通tail riskにmaterial changeがあったとき、または前回の`refresh_triggers`が発火したときだけ更新する。unchanged専用recordは作らない。`valid_until`はwarningの材料であり、screeningの前提条件ではない。triggerの選択と全体導線は[`../operations/decision-cycle.md`](../operations/decision-cycle.md)を正本とする。

## ③ ナレッジ：8 分析レンズ

個別の指標は単体で読まず、以下のレンズに束ねて環境認識に使う（1枚のパネルとして横断的に読む）。操作routingはskill[`macro-analysis`](../../.agents/skills/macro-analysis/SKILL.md)、分析詳細とsource規律は本docを正本とする。

1. **グローバル流動性**：net liquidity ≈ `us.fed_assets` − `us.reverse_repo` − `us.tga`（単位換算注意）。`us.m2` 前年比はリスク資産に約 10 週先行。
2. **実質金利・store-of-value**：`us.real_10y` + `us.breakeven_10y` + `usd_index.broad` + `gold`。名目 = 実質 + 期待インフレに分解。
3. **金融環境の合成**：`us.nfci` を `vix`・`us.move`・クレジット OAS と突き合わせ、slow-burn（広範化前の局所ストレス）を読む。
4. **リスク選好の温度計**：`btc_usd` + `vix` + `credit.us_hy_oas`/`credit.us_ccc_oas` + `us.nfci`。BTC は先行温度計になりやすい（単独 driver にはしない）。
5. **景気サイクル・breadth**：`us.initial_claims` + `us.industrial_production` + `copper` + `us.russell2000` + `us.10y_3m_spread`。`us.sox` は AI/半導体サイクルと日本半導体株の先行ゲージ。
6. **バリュエーション・ERP**：`us.sp500_earnings_yield` − `us.10y` ＝ ERP。益回り < 名目金利（ERP≤0）は警戒域。`us.sp500_cape` で長期割高度。
7. **グローバル中銀の同期**：`us.fed_funds.upper` + `jp.policy_rate` + `ecb.policy_rate`。1 国でなく同期を読む。
8. **エネルギー・地政学**：`wti`/`brent` + `gold`。日本はエネルギー輸入依存が高く（中東 ~95%・ホルムズ ~74%）原油 spike が通貨・スタグフレーションに直結するため `usd_jpy` と併読。

## Material deltaとAIの境界

macro contextはdiscount rate、需要、資金調達、共通tail risk、sizing cautionだけを表す。AIの役割と株主価値の獲得可能性はmacro contextに置かず、企業別decision packetで評価する。

## 接続：判断層にだけ効かせる（screen は macro-blind）

マクロの読みは機械スクリーニングの `run` には接続しない（`run` は財務事実だけを扱う決定論的なエンジンのまま）。効かせるのは判断層だけ：

- **select**（[`./screening.md`](./screening.md)）：material deltaとwarningをcontext-level summaryとして出す。E[r]順位とcandidateの事実層は変えない。
- **research**：material deltaが個別5年期待値へ影響する場合だけ、decision packetのjudgmentへその因果と根拠を残す。マクロを数値ドライバー、採用gate、投入額ルールにはしない。

## 誠実性（honesty firewall）

マクロは標本数がほぼ 1 であり、screening のように多数の銘柄を横断する統計検証ができない。この工程は優位性の数値・統計的有意性・自動売買スコアを出さない。ここで得られるのは再現性と、判断を事実に根付かせる基盤であって、統計的な厳密さではない。

## 参考

- [`../doctrine.md`](../doctrine.md)：思想・柱 2（macroとAIの責務境界）
- [`./screening.md`](./screening.md)：material deltaをwarningとして出すselect
- [`../reference/data-sources.md`](../reference/data-sources.md)：データソース Tier
