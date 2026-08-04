---
name: ledger-record
description: 人間報告の記録。注文結果（open/filled/cancelled/expired）、入出金・income・cost・税、売却約定、年次 outcome を typed draft → 人間確認 → apply --confirmed で ledger へ反映する。
---

# Ledger Record

portfolio ledger の正本は application DB。人間の報告だけを broker fact の入力とし、期日経過や reservation の不在から状態を推定しない。event 意味論と replay は [`portfolio-ledger.md`](../../../docs/reference/portfolio-ledger.md) が正本。

## 共通 lifecycle

1. typed draft command が current DB に束縛した ephemeral draft を exclusive create する。`--out` は repository root 配下の相対 path で渡す（`record-result` / `sell-result-draft` / `market-price-draft` / `holding-review-build` は root 外の絶対 path を拒否する）。
2. 人間が event payload・binding・cash / reservation / holding 差分を確認する。
3. `apply-draft --confirmed` が expected append head・binding・invariant を transaction 内で再検証する。stale なら no-write で draft を再生成する。

session は kind ごとに 1 件（注文結果 = `pending-result`、資金 = `monthly-contribution`、年次 = `annual-outcome`）。

## 人間裁定の記録（proposal decide）

proposal への `approve / defer / reject` は人間の会話報告だけを `uv run baibai-engine proposal --db data/app/baibai.sqlite --market-db data/screening/market.sqlite decide <PROPOSAL_ID> --decision <decision>` で記録する。approve 時は current DB の thesis・price・quantity・expiry・portfolio constraint が再検証され、不一致なら no-write で新しい proposal を作り直す。ledger event が参照した proposal を approved 以外へ変更しない。

## 注文結果

`pending-result` を無関係な market / macro 不足で止めない。

| report | required facts | draft |
| --- | --- | --- |
| `open` | approved proposal ID・ticker・quantity・limit・expiry・sector・時刻 | reservation |
| `filled` | proposal ID・ticker・quantity・price・時刻（reservation 経由なら reservation ID） | execution + remaining |
| `cancelled` / `expired` | proposal ID・reservation ID・時刻（expired は人間が未約定を確認した時刻） | remaining release |

```bash
uv run baibai-engine position record-result --db data/app/baibai.sqlite --proposal-ref <PROPOSAL_ID> \
  --status open --occurred-at <ISO8601+09:00> --ticker XXXX --quantity 100 --sector <SECTOR> \
  --price-guard-yen <LIMIT> --expires-at <ISO8601> --out .cache/ledger/open-draft-<ASOF>.yaml
uv run baibai-engine position apply-draft .cache/ledger/open-draft-<ASOF>.yaml --db data/app/baibai.sqlite --confirmed
```

- 新規 open と reservation なし fill は current approved proposal が必須。migration 由来で binding が null の reservation だけ、人間報告を記録した issue URL を `--proposal-ref` へ渡す。
- partial fill は remaining がある間だけ後続 report を受ける。同一 terminal report は no-change、矛盾 report は hard error。同時刻に複数 reservation が terminal なら `--reservation-id` を反復して 1 draft で release する。

## 売却約定（holding review の判定後）

```bash
uv run baibai-engine position sell-result-draft --db data/app/baibai.sqlite --ticker XXXX \
  --quantity 100 --price-yen <PRICE> --occurred-at <ISO8601> \
  --decision-reference <HOLDING_REVIEW_ID> --out .cache/ledger/sell-draft-<ASOF>.yaml
```

- 約定日が最終 market price 観測から `market_price_max_age_days`（7 日）を超えると price staleness で拒否される — 先に `market-price-draft` を apply する。
- 同時刻・同値の分割約定は 1 件に合算するか `--occurred-at` を分ける。手数料は `--fees-yen`、確定譲渡益税は `--tax-yen`（無い場合はフラグ自体を省略。`0` 指定は拒否される）。保有数量を超える sell は fail-closed。

## 資金・その他 event

```bash
uv run baibai-engine position event-draft --type contribution --event-id <ID> \
  --occurred-at <ISO8601> --amount-yen 100000 --db data/app/baibai.sqlite --out .cache/ledger/event-draft-<ASOF>.yaml
```

`contribution / withdrawal / income / cost / tax_confirmed` は確認した事実ごとに 1 event。risk override は `override-draft`、tax estimate 設定は `meta-draft`。購入や screening を強制しない。

## 年次 outcome

ledger event を JPX 営業日 close まで再生し、配当込み TOPIX と比較して resolved 結果だけを publish する。period・source・cash-flow basis 不足の `unresolved` は不足理由を確認し、正本へ保存せず同じ期間を再実行する。短期結果だけで policy を変えない。

## 停止条件

- 人間報告・approved proposal・required field がない。
- draft 生成後に append head・proposal・reservation・price/meta row が変わった（no-write で再生成）。
- event が future-dated、または reservation state と矛盾する。
