---
title: "月次運用サイクル runbook"
summary: "単一ループを月次で 1 周する e2e 手順の正本。資本更新 → マクロ環境認識 → screening → select → research → 取引提案 → 発注記録 → 保有レビュー → 較正の増分再計測。"
doc_type: operation
status: active
last_reviewed: 2026-07-09
related_docs:
  - "../workflow/README.md"
  - "../portfolio-management.md"
  - "./improvement-loop.md"
---

# 月次運用サイクル runbook — 単一ループの e2e 導線

[`../doctrine.md`](../doctrine.md) §2 の単一ループを**月次で 1 周する**ときの e2e 手順。各工程の詳細な正本は [`../workflow/`](../workflow/) の各 doc と skill（`macro-analysis` / `ai-value-bargain-selection` / `ir-research` / `financial-pro-review` / `tradingview-open`）にあり、本 doc は「どの順で・何を確認して・どこに成果物を残すか」の導線を 1 本で持つ。

役割分担: **AI がマクロ分析・screening・個別リサーチ・提案の作成までを主導し、人間が提案を判断して発注する**。timing は月初〜月中を基本にし、大型イベント（FOMC / 日銀会合）の直後に macro を読み直せる日取りを選ぶ。

## 0. 資本と保有の現況確認

```bash
ls records/04-position/*/*/*.md                      # 現保有・約定待ち
uv run baibai-loop-position benchmark                # 保有の対 benchmark 相対リターン
```

- `real_capital_yen` を月次積立（[`../portfolio-management.md`](../portfolio-management.md): 月 +40 万、投下は 20–30 万 + 暴落余力）に応じて更新する。
- 約定待ち注文（`execution_state: submitted`）の期限・撤回条件を確認する。期限切れは `expired` に更新し、今月の選定で再評価する。

## 1. マクロ環境認識（skill: `macro-analysis`）

```bash
uv run baibai-loop-macro list                        # series registry
# 判断に使う主要 series は refresh してから読む（get --latest は鮮度窓内の cache を優先する）
uv run baibai-loop-macro refresh <series> --start <直近> --end <today>
```

- `refresh` は一時的な provider failure を 1 回 retry する。再失敗した series は upstream 停止・HTML/CSV/XLSX 構造変更・credential 欠落のいずれかとして扱い、error と source を確認してから再実行する。
- 世界情勢 → 日本経済 → 個別資産の順に読み（[`../doctrine.md`](../doctrine.md) §7）、**リスク姿勢（ディフェンシブ / リスクオン）とセクター tilt** に落とす。深さの基準・敵対的 self-check は skill `macro-analysis` の品質ゲートを通す。
- 成果物: `records/01-macro-context/YYYY/MM/macro-context-YYYY-MM-DD-<slug>.yaml`（schema: `records/_schemas/macro-context.json`）。`as_of` は screening asof（最新の完全営業日）に合わせる。
- `uv run baibai-loop-validation --target macro-context` を通す。

## 2. Screening（機械抽出）

```bash
ASOF=<最新の完全営業日 YYYY-MM-DD>
uv run baibai-loop-screening bootstrap-cache --asof "$ASOF"
uv run baibai-loop-screening extract-edinet-metrics --asof "$ASOF"    # 数分かかる
uv run baibai-loop-screening verify-cache-coverage --asof "$ASOF"
uv run baibai-loop-screening run --asof "$ASOF"
```

`run` は cache-only / point-in-time で、不足があれば fail する（provider fallback しない）。J-Quants throttling 時は直近の完全営業日へフォールバックする。

## 3. Select（研究候補の選定）

```bash
uv run baibai-loop-screening select --asof "$ASOF" --detail full > .cache/select-$ASOF.yaml
# 広域 triage では .cache の一時 rules だけで output.research_selection_target_max を引き上げる
cp records/_config/screening-rules/2026-07-06T000000+0900.yaml \
  ".cache/screening-rules-triage-$ASOF.yaml"
# .cache/screening-rules-triage-$ASOF.yaml の output.research_selection_target_max を必要件数へ編集する
uv run baibai-loop-screening select --asof "$ASOF" --top 20 \
  --rules-path ".cache/screening-rules-triage-$ASOF.yaml" \
  --detail full > ".cache/select-triage-$ASOF.yaml"
```

- recommendations は **機械 E[r] 降順**（成分分解 + FV アンカー付き）。playbook screen は `evidence_hits` / `selection_playbook` の注記であり、evidence がない候補も `selection_playbook: null` のまま E[r] 上位なら入る。durability 注記・sector / playbook 集中度・E[r] 成分を確認する。
- 広域 triage は `--top` だけでなく、本番 rules YAML を `.cache/screening-rules-triage-$ASOF.yaml` にコピーして `output.research_selection_target_max` を必要件数へ引き上げ、その一時 rules を `--rules-path` で渡す。本番 rules と calibration store は、較正済み baseline と再現性のある本番順位を保つために触らない。
- 既存保有 ticker と構造衰退業種は skill 側 post-filter で除外する（`select` に除外フラグはない）。
- `price_change_60d` / percentile が極端な候補は corporate action を確認する（AP-03）。
- `dps_actual_annual / dps_forecast_annual > 1.5` の候補は、E[r] carry を予想配当基準で読み替える。IR 対象に進める前に、株式分割・併合などの corporate action（AP-03）、特別配当、減配ガイダンスを一次 IR / 適時開示で確認し、実績配当と予想配当の乖離が持続的な carry ではない可能性を潰す。

## 4. Research（個別リサーチ、skill: `ai-value-bargain-selection` / `ir-research`）

- 上位 3–5 銘柄を一次 IR で深掘りし、FV・RR・期待利回り・塩漬け耐性・反対仮説・invalidation を確認する（[`../workflow/research.md`](../workflow/research.md)）。
- 手動 FV は select が転記する機械アンカー（`fv_sector_median_yen` / `fv_self_range_yen`・E[r] 成分）を出発点にし、乖離理由を本文に書く。
- 成果物: `records/03-thesis/YYYY/MM/YYYY-MM-DD-<ticker>-<playbook>.md`。`uv run baibai-loop-validation --target thesis` を通す。
- 公開前に skill `financial-pro-review` の運用規律レビューを通す。

## 5. 取引提案（GitHub Issue）

- 採用銘柄を「**どの銘柄を・いくらで・何株**」の売買提案 issue に落とす（提案が records ディレクトリを持たない設計。承認結果は position record に落ちる）。
- 必須: thesis への参照、指値と数量（単元丸め）、cap 確認（単一銘柄 4–6% / sector 30–40% / playbook 35% / ADV 5%、分母 `real_capital_yen`）、注文期限、撤回条件（期限内の大型イベント・macro refresh trigger）、**TradingView リンク**（skill `tradingview-open`）。
- 人間が判断・発注する。

## 6. 発注記録と約定監視

- 発注したら `records/04-position/YYYY/MM/YYYY-MM-DD-<ticker>.md` を `execution_state: submitted` で作る（約定価格を推定で埋めない。AP-09）。
- 約定・期限切れ・撤回のイベントで record を更新する（[`../workflow/position.md`](../workflow/position.md) の状態遷移）。
- `uv run baibai-loop-validation --target position` を通す。

## 7. 保有レビュー（月次 + 決算後）

```bash
uv run baibai-loop-position calibration --asof "$ASOF" > ".cache/position-calibration-$ASOF.yaml"
```

- 各保有の `review_valuation`（FV・現値・valuation zone・hold/add/sell）を月次で更新する。割高ゾーン到達は全売り、割安継続は保有 / 買増し。
- `calibration` の YAML は、open position ごとの entry 見積り、現在リターン、benchmark 相対リターン、FV gap、draft `valuation_zone` / `action`、aggregate、coverage を持つ。draft `action` は `review_valuation` と `estimate_calibration` の下書きであり、自動の exit 判断ではない。FV や J-Quants bars が欠ける場合は該当値を `null` として扱い、warning と coverage を確認する。
- 決算後レビューは `task:earnings-review` issue（[`./task-runbook.md`](./task-runbook.md)）で漏れを防ぎ、判断は records に戻す。
- exit・決算後に `estimate_calibration`（entry 見積り vs 実現）を更新する。

## 8. 較正の増分再計測（改善ループへの接続）

```bash
uv run baibai-loop-screening calibration-build --start 2022-09-01 --end <直近の完全月末>
uv run baibai-loop-screening calibration-evaluate --out .cache/calibration-eval-monthly.yaml
```

- 新しい cohort の解決分を取り込み、**採用済み改善の監視事項**（直近の estimate-calibration 系 report 末尾）を確認する。
- 系統的な劣化・新しい観察が出たら issue に登録し、[`./improvement-loop.md`](./improvement-loop.md) のサイクルへ渡す。

## 月次チェックリスト

- [ ] `real_capital_yen` 更新・約定待ち注文の棚卸し
- [ ] macro-context 更新（refresh → 8 レンズ → validation）
- [ ] screening pipeline（bootstrap → extract → verify → run → select）
- [ ] 上位候補の research（一次 IR・FV/RR/耐性・pro review）
- [ ] 取引提案 issue（指値・数量・cap・期限・撤回条件・TradingView リンク）
- [ ] position record（submitted で作成、約定で更新）
- [ ] 保有の `review_valuation` 全件更新
- [ ] calibration 増分 build + evaluate、監視事項の確認
- [ ] `uv run baibai-loop-validation` 全体通過
