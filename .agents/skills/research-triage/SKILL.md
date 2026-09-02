---
name: research-triage
description: machine runnerが固定したReview Setをresearch / skipへ分類し、人間がResearch Setを確定するまで進める。深掘りはresearch skill。
---

# Research Triage

## 目的

4つの価値評価法からmachineが発行した有限のReview Setについて、Fundamental Researchの時間を使う価値がある対象だけを判断する。`research`は買い推奨ではなく、`skip`も正常な結論である。AIはbroker操作へ進まない。

## 通常実行

```bash
uv run baibai-batch analysis run
# 過去日の手動再実行だけ --asof YYYY-MM-DD
```

この1 commandが営業日判定、daily machine job、AI不要条件、Research Triage入力、strict AI result、engine publisher、必要なOperation開始を所有する。ID転記、workspace探索、`status / check / publish`の選択、成功logの確認は行わない。

次はmodel process 0で終了する。

- 非営業日、Review Setなし、Review Set 0件
- 進行中の別Operation
- exact Review Setのcanonical Research Triageが既に存在する
- 必須machine inputの欠損・破損

`awaiting_human`または`published_awaiting_human`なら、表示された`research`候補から人間がResearch Setを確定するまで`research` skillへ進まない。`published_all_skip`と空Review SetではOperationを作らない。

## 判断契約

判断規則のSSOTは[`TRIAGE_POLICY`](../../../batch/src/baibai_batch/analysis/policy.py)である。manualに意味確認が必要な場合もこの短い定数だけを読み、scheduled runnerへ本skill、runbook、CLI help、raw logを渡さない。

- AI出力は`ticker`、`verdict`、`rationale`、`research_question`、`key_risk`だけとする。
- machine ranking、Review Set membership/order、Nomination、candidate snapshotを変更しない。
- Macro ContextとE[r]は参考文脈であり、単独gateにしない。
- unknownを否定事実へ変換しない。

## 失敗時

失敗runは同じcommandをfreshに再実行する。旧workspaceのresume、cache、stage選択は行わない。canonical Research Triageが既に発行されていれば次回runがmodelと重複publishをskipする。通常stdoutだけで判断できない失敗時だけ、表示されたprivate `log_path`を読む。

## 正本

- [`analysis-operations.md`](../../../docs/reference/analysis-operations.md)
- [`screening-runtime.md`](../../../docs/reference/screening-runtime.md)
- [`portfolio-management.md`](../../../docs/portfolio-management.md)
- public CLI `--help`
