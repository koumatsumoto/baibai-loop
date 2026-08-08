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

sourceは`stores/application/baibai.sqlite`（SHA-256 `d99b258ed4cb4c763d9bf154d792aef092618e2627d2e6afce4b1c665c5caa62`）。OP3 rejected 36件、bargain assessment reject / defer 5件を確認した。

### OP3 遡及分類

| as_of | class | tickers | 件数 |
| --- | --- | --- | ---: |
| 2026-07-17 | `price_already_converged` | 7595, 5445, 6345, 3608, 6436, 4716, 3405, 7575 | 8 |
| 2026-07-17 | `structural_decline` | 8291, 7915 | 2 |
| 2026-07-17 | `other` | 6417, 5021 | 2 |
| 2026-07-28 | `one_off_earnings` | 4849, 4887, 7595, 3608, 4716 | 5 |
| 2026-07-28 | `price_already_converged` | 6436, 2267 | 2 |
| 2026-07-28 | `structural_decline` | 8291, 7915 | 2 |
| 2026-07-28 | `other` | 5021, 8252 | 2 |
| 2026-07-28 | `provision_or_writedown` | 3405 | 1 |
| 2026-07-29 | `one_off_earnings` | 4849, 4887, 7595, 3608, 4716 | 5 |
| 2026-07-29 | `other` | 5021, 2337, 8252, 3405 | 4 |
| 2026-07-29 | `price_already_converged` | 6436, 7944 | 2 |
| 2026-07-29 | `structural_decline` | 7915 | 1 |

| class | 件数 | 構成比 |
| --- | ---: | ---: |
| `price_already_converged` | 12 | 33.3% |
| `one_off_earnings` | 10 | 27.8% |
| `other` | 8 | 22.2% |
| `structural_decline` | 5 | 13.9% |
| `provision_or_writedown` | 1 | 2.8% |
| **合計** | **36** | **100.0%** |

`other`は8/36（22.2%）で事前登録した30%関門以下だったため、初期taxonomyを採用する。`other`の主因は投資対象外業種、macro event中のexposure、高leverage・与信/金利感応などで、現標本では新classを増やすほど単一の塊にならなかった。`governance_accounting` / `supply_demand_liquidity` / `data_quality` / `event_wait`はOP3の主因として0件だったが、将来の観測とresearch deferを表せるため初期enumに残す。

### Research 遡及分類

| assessment | ticker | disposition | class |
| --- | --- | --- | --- |
| 2026-07-28 carry-durability | 6345 | reject | `price_already_converged` |
| 2026-07-28 carry-durability | 6088 | defer | `event_wait` |
| 2026-07-29 three-lane-no-buy | 7943 | reject | `structural_decline` |
| 2026-07-29 three-lane-no-buy | 6345 | reject | `price_already_converged` |
| 2026-07-29 three-lane-no-buy | 6458 | reject | `price_already_converged` |

分布は`price_already_converged` 3/5、`event_wait` 1/5、`structural_decline` 1/5。全5件を判別できた。assessment導入前のthesis-only defer（4432、2026-07-14）はQ2でorganic成長とmarginを確認する判断であり、補足分類は`event_wait`としたが、事前登録どおり上記集計には混ぜていない。

### 還流判断

頻度1位の`price_already_converged`は、既存のFV anchorと現値から機械判定できる。自動除外・E[r]・rankingを変えず、FV anchorに対する上値不足をselection longlistとUIへwarning表示する[#749](https://github.com/koumatsumoto/baibai-loop/issues/749)を最初の変換候補として起票した。2位の`one_off_earnings`は既存`forecast_special_gain_flag`が既に一部を表面化しているため、最初の追加実装には選ばない。
