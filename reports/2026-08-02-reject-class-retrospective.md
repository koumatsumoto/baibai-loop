# 棄却理由 taxonomy 遡及分類

価値tier: T1〜T3 — OP3 と一次 research の棄却理由を列挙可能にし、頻出する機械化可能な失敗型を screening warning の改善候補へ接続する。

## 事前登録

この節は遡及分類の集計前に固定する。対象は 2026-08-01 までに application DB へ保存された次の判断とする。

- OP3: 全 shortlist の `decision: rejected` entry。サイクル別・class 別に集計する。
- research: bargain assessment の `disposition: reject | defer` lane。shortlist と工程が異なるため別集計にし、合算しない。
- bargain assessment 導入前の thesis-only 判断は research 集計へ混ぜず、該当があれば補足として記録する。

各判断には、自由記述の正本を変えず、結論を成立させた主因を 1 class だけ付ける。複数の懸念がある場合は、記述からその懸念を除くと reject / defer が成立しなくなる理由を主因とする。分類は次の優先ルールで再現可能にする。

| class | 判定ルール |
| --- | --- |
| `one_off_earnings` | 特別益、一過性利益、一時的な高配当など、持続しない利益・還元が主因 |
| `provision_or_writedown` | 引当、減損、評価損などの計上または期落ち待ちが主因 |
| `structural_decline` | 需要縮小、顧客依存、競争力低下など事業構造の毀損が主因 |
| `governance_accounting` | 統治、会計、開示品質、資本配分への信頼不足が主因 |
| `price_already_converged` | 正常化利益や FV を認めても現値から必要な上値・期待利回りが残らないことが主因 |
| `supply_demand_liquidity` | 出来高、需給、売買可能性が主因 |
| `data_quality` | machine input または計算値と実態の不一致が主因 |
| `event_wait` | dated event の結果待ちが defer の主因 |
| `other` | 上記のどれも主因を表せない |

taxonomy は OP3 の `other` が 30% 以下なら初期運用へ採用する。30% を超える場合は enum を確定せず、`other` の内訳から taxonomy を改訂して再分類する。research は標本が小さいため率で採否せず、判別不能な事例を個別に記録する。

採用時は OP3 の頻度 1 位を最初の還流候補とする。その class が自由記述による人間判断を本質とし機械 warning に適さない場合は理由を記録し、頻度順で次の機械化可能 class を選ぶ。変換は自動除外や ranking 変更を行わない別 issue として起票する。

## 結果

集計後に追記する。
