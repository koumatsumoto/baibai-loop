---
name: market-context-assessment
description: 人間の明示開始から保存済み入力を固定し、小さな市場環境判断をreview後に発行する。
---

# Market Context assessment

人間の明示依頼で市場環境の判断を作る。経済・金利・市場・業種と個別調査へのmaterialな含意を扱い、章数・シナリオ・主観確率・scorecardの件数を満たすための文章を作らない。日次更新・staleだけでAI執筆を起動しない。

## 手順

1. 対象日`as_of`と入力確定時刻`known_at`を固定する。通常は今回の入力確定時刻、当時の分析入力を調べる場合は人間が指定した過去cutoffを使う。前営業日の評価を週末に発行でき、対象日末を一律cutoffにはしない。保存済みReading・市場/業種/個別状態と一次資料を読む。`macro get`は取得を伴うため、保存済み読取と混同しない。正本と同期は[Ops Maintenance](../ops-maintenance/SKILL.md)に従う。
2. 固定入力の出典・利用可能時刻と、要約・重要変化の根拠・反証/不確実性・調査とリスクへの含意を書く。変化が小さい場合や欠測をそのまま記録する。定量状態はL1/L2を引用し、AIが同じ値を別の正本として保存しない。形は[model](../../../engine/src/baibai_engine/market_context/assessment/models.py)、意味は[reference](../../../docs/reference/macro.md#assessment)に従う。
3. [判断文書の編集](../../../docs/reference/judgment-writing.md)に従って編集し、authorと別sessionのreviewerへ固定draftとsourceを渡す。source、因果、反証、含意を確認する。最大3巡で未解決なら発行せず人間へ上げる。機械checkを意味のreviewの代わりにしない。
4. review PASSまたは人間のdraft承認後にcheckし、既存headを確認してCAS発行する。以下のfileと非秘密IDは実値へ置換する。

   ```bash
   uv run baibai-engine market-context assessment publish /tmp/assessment.json --check
   uv run baibai-engine market-context assessment head
   ```

   headが存在する場合:

   ```bash
   expected_head='確認したheadのcontext_id'
   uv run baibai-engine market-context assessment publish /tmp/assessment.json --expected-head "$expected_head"
   ```

   headがnullの場合だけ`--expected-head`を省略する。同じIDは同じ本文・発行状態の再試行だけを許す。CAS競合でheadだけ更新して無条件再試行しない。
5. 発行IDを指定して本文を読戻す。cloud反映はOps Maintenanceのapplication反映へ進む。観測storeの`push-macro`だけでは判断の反映にならない。

   ```bash
   context_id='発行結果のcontext_id'
   uv run baibai-engine market-context assessment show --context-id "$context_id"
   ```

履歴ID閲覧は分析入力の適格性と別であり、旧本文・入力・引用をそのまま返す。分析用の選択は次のcommandで対象日と利用可能時刻を指定する。

```bash
uv run baibai-engine market-context assessment latest --asof 2026-07-17 --known-at 2026-07-18T13:00:00+09:00
```

市場判断は調査・価値判断・購入量やリスクの検討に接続する入力であり、最終注文は人間が確認する。旧固定章report・handoff・scorecardを新規作成する経路は使わない。保存済み履歴を共通projectionで置き換えたり、旧runner `input.json`を上書き再生成しない。
