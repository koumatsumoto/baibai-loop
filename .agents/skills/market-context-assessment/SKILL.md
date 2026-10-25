---
name: market-context-assessment
description: 人間の明示開始から市場入力を固定し、重要な環境変化と含意を独立Review後に発行する。
---

# 市場環境判断（Market Context Assessment）

明示依頼で共通の市場判断を作る。日次更新やstaleだけでAI執筆を起動しない。[市場環境](../../../docs/reference/market-context.md#assessment)が意味を所有する。

1. 対象日as_ofと入力確定時刻known_atを固定し、保存Reading・市場/業種/個別状態と一次資料を読む。取得が必要なら[運用](../../../batch/OPERATIONS.md)へ進む。
2. [市場環境判断の契約](../../../docs/reference/market-context.md#assessment)に沿って重要な水準・変化、反証/不確実性と個社への含意をまとめる。
3. 作者と別sessionのreviewerがsource・因果・反証・含意を確認する。数値・単位・日付・限定を保って編集し、新たな根拠や判断を採用したら再Reviewする。最大3巡で未解決なら発行せず人間へ上げる。
4. Review PASSまたは人間のdraft承認後、known_atを保ち、初回発行直前の実時刻をpublished_atへ設定してcheckする。headを読んでCAS発行する。

```bash
uv run baibai-engine research market-context publish <DRAFT> --check
uv run baibai-engine research market-context head
uv run baibai-engine research market-context publish <DRAFT> --expected-head <CONTEXT_ID>
```

headがnullの場合だけexpected-headを省略する。同じIDは同内容の再試行に使い、published_atを作り直さない。結果不明ならIDで読戻し、CAS競合でheadだけを更新して再送しない。

```bash
uv run baibai-engine research market-context show --context-id <CONTEXT_ID>
uv run baibai-engine research market-context latest --asof <YYYY-MM-DD> --known-at <AWARE_DATETIME>
```

発行IDの本文を読戻す。[保存履歴](../../../docs/reference/market-context.md#保存履歴)を保ち、本番反映は[application反映](../../../batch/OPERATIONS.md#application-db-を反映する)へ進む。push-indicatorsだけではL3判断は反映されない。
