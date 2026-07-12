---
title: "Workflow — position (execution & holding)"
summary: "decision packetに基づく手動注文、ledgerへの約定反映、holding review、portfolio outcomeをつなぐ工程。"
doc_type: workflow
status: active
last_reviewed: 2026-07-12
---

# Workflow — 執行・保有

この工程は、decision packetの購入判断を人間のbroker操作へ渡し、確認済みの注文・約定・現金・保有をcanonical portfolio ledgerへ記録する。自動発注はしない。資本の正本は`records/04-position/portfolio-ledger.yaml`、注文と約定の整合はexecution lifecycle、保有見直しはholding reviewで扱う。

## 新規注文

1. decision packetを`baibai-loop-decision`で再計算し、source freshness、5年base scenario、永久損失7軸、独立reviewを確認する。
2. `uv run baibai-loop-position ledger`でavailable cash、active reservation、保有、concentration warningを確認する。warningを受け入れる場合は理由と期限を持つhuman overrideをledgerに記録する。
3. packet hashに束縛したexecution lifecycleへ、human-confirmed intent、brokerへ送信したorder、broker-confirmed executionを時系列で記録する。約定価格を推測で埋めない。
4. lifecycleのreservation / execution / releaseと対応するledger eventを照合する。期限切れ・取消・broker拒否はreleaseを明示し、reserved cashを暗黙解放しない。

契約の詳細は[`../reference/execution-lifecycle.md`](../reference/execution-lifecycle.md)、資本eventとreconciliationは[`../reference/portfolio-ledger.md`](../reference/portfolio-ledger.md)を正本とする。

## 保有見直し

決算発表後またはmaterialな変化があった保有だけを対象にholding reviewを更新する。価格下落だけでは売らず、永久損失リスク、証拠鮮度、現値起点の5年期待値、税引後の代替機会費用から`hold / add / reduce / exit`を提案する。FV到達はreview triggerであって自動売却ではない。

既存保有に初めてreviewを作る場合は、現在の一次情報と現値からholding decision packetを作る。根拠を推測で復元しない。

```bash
uv run baibai-loop-position holding-review --input records/04-position/YYYY/MM/YYYY-MM-DD-XXXX-review.yaml
uv run baibai-loop-validation --target holding-review
```

## 成果測定

portfolio全体の結果はledger eventをJPX営業日closeまで再生し、TOPIX配当込みの同期間returnと比較する。これはportfolioの実現結果を確認する計測であり、短期screen最適化には使わない。

```bash
uv run baibai-loop-position outcome --benchmark-observation records/04-position/benchmarks/topix-1y.yaml
```

## 参考

- [`./research.md`](./research.md)：購入判断と発注上限
- [`../operations/decision-cycle.md`](../operations/decision-cycle.md)：trigger別の実行順序
- [`../reference/holding-review.md`](../reference/holding-review.md)：保有判断の算術
