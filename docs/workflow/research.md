---
title: "Workflow — research"
summary: "個別銘柄リサーチをdecision packetへ固定し、フェアバリュー、5年シナリオ、永久損失、反証、発注上限を再計算可能にする工程。"
doc_type: workflow
status: active
last_reviewed: 2026-07-12
---

# Workflow — 個別銘柄リサーチ

[`./screening.md`](./screening.md) のcandidatesを起点に個別銘柄を調べ、フェアバリュー、5年期待総合リターン、永久損失リスク、反証を[`../reference/decision-packet.md`](../reference/decision-packet.md)のdecision packetへ固定する。AIは下書き・比較・再計算を行い、人間が購入判断とbroker操作を行う。

## 選定プロセス

1. `uv run baibai-loop-screening select` の`recommendations`、E[r]成分、durability注記、sector集中度を確認する。個別の事実確認は`ticker-profile --ticker XXXX`から始める。
2. 一度に扱う候補を3〜5銘柄へ絞り、一次情報で事業構造、利益・cash flow、資本政策、希薄化、競争環境を確認する。
3. 割安の原因仮説と、構造的衰退・資金繰り・希薄化・顧客集中・会計を含む反証を並べる。macro material deltaは個別の5年期待値を変える場合だけ根拠へ接続する。
4. 3年・5年のbear/base/bull scenario、フェアバリュー、永久損失7軸、AI value-capture、source lineageをdecision packetへ記録する。
5. packetがreadyになった場合だけ、5年base scenarioと必要CAGRから最大許容価格を再計算し、ledger snapshotのcash・reservation・concentration warningと合わせて`buy_now / shallow_limit / deep_limit / defer`を比較する。

## 判断原則

- 割安は機械的なvaluation rankingと個別フェアバリューの二段で判定する。ピーク利益をそのまま外挿せず、利益の正常化とその前提をscenarioへ置く。
- リスクリワードはFVまでの上昇余地と永久的な元本毀損リスクの比較であり、短期の価格損切り幅ではない。価格下落だけを売却理由にしない。
- 塩漬け耐性は購入前のhard gateである。営業cash flow、バランスシート、借換、希薄化、収益基盤を確認できない候補は、割安でも通常sizingの購入提案へ進めない。
- AIはテーマではなく企業別のvalue-captureを評価する。AI需要を採用理由にせず、競争優位、価格決定力、必要capex、顧客交渉力、disruptionを証拠とともに判断する。
- FV到達はholding reviewのtriggerであり自動売却ではない。保有の`hold / add / reduce / exit`は[`../reference/holding-review.md`](../reference/holding-review.md)のthesis healthと税引後代替比較で決める。

## 検証

```bash
uv run baibai-loop-decision records/03-thesis/YYYY/MM/YYYY-MM-DD-XXXX-decision.yaml
uv run baibai-loop-validation --target decision-packet
```

## 参考

- [`./screening.md`](./screening.md)：候補抽出
- [`./position.md`](./position.md)：注文・約定・保有見直し
- [`../portfolio-management.md`](../portfolio-management.md)：資本・集中度・余力
- [`../reference/decision-packet.md`](../reference/decision-packet.md)：packet契約と発注上限の算術
