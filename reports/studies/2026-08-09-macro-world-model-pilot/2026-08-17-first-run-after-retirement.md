# 撤収後 初回運転の計測（as_of 2026-08-17）

`macro-context` skill を PR #970 / #971 の新契約で初めて通した記録。#969 の採否基準 T2 / T4 / T5 を判定するための 1 回限りの計測であり、常設の記録義務にはしない。

publish した revision: `macro-context-2026-08-17-energy-lock-and-yen-decoupling`

## pass 別 wall-clock

| pass | 名前 | 分 |
|---|---|---|
| 1 | prior-blind-preflight | 0.8 |
| 2 | refresh-and-reading | 4.7 |
| 3 | force-hypotheses-and-primary-sources | 21.2 |
| 4 | market-snapshot | 4.2 |
| 5 | core-sections | 14.7 |
| 6 | synthesis | 2.3 |
| 7 | scorecard-settle | — |
| 8 | connection | 2.8 |
| 9 | thesis-impact | — |
| 10 | self-check | 8.9 |

- 合計 **59.6 分**（3578 秒）。これは**下限**である
- pass 7 は中断（約 5 時間 25 分）を窓に含むため除外、pass 9 は pass 10 と窓を共有するため除外した。推測値で埋めていない
- 中断そのものは合計に含まない

## T5: 手順 7 の開始が結論確定の後だったか — **満たす**

- 17:27:53 に core 10 セクション・synthesis（4 力 2 相互作用）・connection を書き終え、17:28:27 に組み上げて `publish --check: ok` を得た
- **17:28:42** に `date` を印字してから前回レポートを開いた（pass 7 の `started_at`）
- 中断前に確定した結論（stance・確率・4 力・全 scenario 条件）は pass 7 の後に書き換えていない

## T4: 手順 3 が cycle 2 の evidence-packs 21 分を下回ったか — **未達**

21.2 分で下回っていない。事前登録の閾値なので結果を見て動かさない。ただし**内訳は入れ替わっている**。

- tool 切り分けの往復は **0 回**。`data-sources.md` §取得失敗の切り分け を先に読み、最初から curl + browser UA で入ったため、WebFetch と curl のどちらが遮断されているかを確かめる工程が消えた
- 21.2 分の中身は **URL 発見**（MOF が `feint` でなく `feio` 等）と PDF / CSV からの数値抽出
- #969 の完了条件が定める「削減しなければ T4 の内容を直す」に従い、確定した 4 本の URL 規則・cp932・ECB timeout を `data-sources.md` へ追記した

取得の実測: 記事 24 件 fetch / 23 件 ok / 1 件 failed、403・404・302・timeout の再試行 **14 回**（404 が 10 件で、その全てが URL 発見）。

## T2: 独立レビューが指摘を出したか — **満たす**

author（汚染なし subagent）と別 role のレビュアが縦読みし、**判断面 2 件 + 機械面 1 件**を検出した。`publish --check` は全工程で `ok` を返しており、構造 gate はいずれも検出しない。

1. **qualifier carry-through（必須反証 1）** — `rates_policy` の fact が「3 極（FRB・日銀・ECB）が同じ原因（中東情勢）を挙げて据え置き」と書いたが、日銀で成立しない。引用済み一次資料は反対理由が需要側、7 月展望は Dubai 原油 80→70 の**低下**前提で、draft 自身が別の 3 箇所でその低下前提を「正常化の基本線」の根拠に使っていた。上流に反証事実がありながら下流で丸められた形。summary 冒頭と最上位の力に届いていた
2. **窓の含意（必須反証 3）** — `liquidity_credit` の judgment が 3 年窓（786 観測）の CCC OAS percentile 0.95 と 10 年窓の倒産件数 percentile 1.00 を同一結論の共同根拠として並置した。同じ系列に別の箇所では「3 年窓」注記があり、1 文だけ落ちていた
3. **引用の裏づけ（機械）** — 1 の修正で足した BOJ 記述が、その fact の `source_ids` に BOJ input を持たなかった。author が検査を実装して 118 文へ流したところ **14 件**の同種の欠落が見つかり、全件解消した

必須反証 4（消えた force の明示）と 5（`usd_jpy`・`jp.10y` の機械監視）は初回から満たしていた。前回 head を壊した欠陥（介入の断定、real / nominal 混在）は**再発していない**。

## 副産物として確認できた gate の穴

- **scorecard が as_of 時点で既に成立している条件を拒否しない。** 前回 head の唯一の `met` は前回 as_of 時点で既に真だった条件で、較正情報を持たなかった
- **`publish --check` は引用が解決するかは見るが、主張が裏づけられているかは見ない。** 上記 3 の 14 件がその実例
