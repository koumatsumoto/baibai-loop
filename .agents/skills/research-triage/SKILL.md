---
name: research-triage
description: machine runnerが固定したReview Setをresearch / skipへ分類し、人間がResearch Setを確定するまで進める。深掘りはresearch skill。
---

# Research Triage

## 目的

4つの価値評価法からmachineが発行した有限のReview Setについて、Fundamental Researchの時間を使う価値がある対象だけを判断する。`research`は買い推奨ではなく、`skip`も正常な結論である。AIはbroker操作へ進まない。

## 通常実行

```bash
batch/scripts/pull.sh
uv run baibai-batch analysis run
# 過去日の手動再実行だけ --asof YYYY-MM-DD
batch/scripts/publish.sh
```

`pull.sh`はR2のcanonical machine storeを取得し、`analysis run`は対象`as_of`のlatest canonical Review Set解決、AI不要条件、Research Triage入力、strict AI result、engine publisherだけを所有する。`analysis run`内ではScreening RunやReview Setを生成せず、対象日にReview Setが無ければ前営業日へfallbackしない。`publish.sh`はapplication DBをuploadしてcloud materializeを起動する。成功artifactからのID探索、workspace探索、`status / check / publish`の選択、成功logの確認は行わない。Triage publishではOperationを開始しない。

次はmodel process 0で終了する。

- 非営業日、Review Setなし、Review Set 0件
- exact Review Setのcanonical Research Triageが既に存在する
- 必須machine inputの欠損・破損

`awaiting_human`または`published_awaiting_human`なら、stdoutのcanonical `research_triage_id / as_of`とpriority順の全候補・理由・調査質問・主要リスクを示し、その`research`候補から人間がResearch Setを確定するまで`research` skillへ進まない。

選択後は出力されたexact IDを`research prepare --research-triage-id`へ、選んだtickerだけを`--ticker`へ渡す。過去日実行をlatest別日のTriageへ置き換えない。

active Operationはdaily Triageを止めず、新しいResearch開始だけを止める。Triage結果にかかわらず、このskillはOperationを作らない。

## 判断契約

判断規則のSSOTは[`TRIAGE_POLICY`](../../../batch/src/baibai_batch/analysis/policy.py)である。manualに意味確認が必要な場合もこの短い定数だけを読み、scheduled runnerへ本skill、runbook、CLI help、raw logを渡さない。

- AI出力は`ticker`、`verdict`、`priority`、`rationale`、`research_question`、`key_risk`だけとする。
- machine ranking、Review Set membership/order、Nomination、Review Set Entry snapshotを変更しない。
- 4 Approachの仮説をprimary authorityとし、relative weaknessは低priorityで表す。絶対的なResearch価値が無い場合だけskipにする。
- Macro ContextとE[r]は参考文脈であり、単独gateにしない。E[r]はsecondary return priorで、高低・負値・欠損だけからverdictやpriorityを決めない。
- unknownを否定事実へ変換しない。問いは「何を調べれば判断が変わるか」。購入時の完全な証拠を入口で要求せず、株価上昇、無配、回復可能な赤字だけで除外しない。

## 失敗時

失敗runは同じcommandをfreshに再実行する。旧workspaceのresume、cache、stage選択は行わない。canonical Research Triageが既に発行されていれば次回runがmodelと重複publishをskipする。通常stdoutだけで判断できない失敗時だけ、表示されたprivate `log_path`を読む。

## 正本

- [`analysis-operations.md`](../../../docs/reference/analysis-operations.md)
- [`screening-runtime.md`](../../../docs/reference/screening-runtime.md)
- [`portfolio-management.md`](../../../docs/portfolio-management.md)
- public CLI `--help`
