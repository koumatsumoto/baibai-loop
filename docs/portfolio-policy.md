# Portfolio Policy

この document は、Baibai-Loop の portfolio policy を人間が理解するための説明文書です。具体的な資本額、position size、concentration cap、board lot、kill switch など validator-visible な閾値は `src/baibai_loop/policy_config.py` で管理します。

この policy は自己運用の判断統制に使う portfolio policy であり、投資助言や自動売買ルールではない。

## Swing-first / long-hold-capable value principle

Baibai-Loop の主戦略は、5-40 営業日の中期スイングトレードで一時的に過小評価された銘柄を買い、短期から中期で価格回復・catalyst・需給改善が出た場合に利確することである。

ただし、想定通りに上昇せず売却タイミングを逃した場合でも、長期保有へ切り替えられる銘柄を優先する。そのため、採用候補は valuation の割安さだけでなく、長期保有になっても耐えられる可能性が高い balance sheet、cash flow、流動性、借換リスク、収益基盤を確認する。ここでいう長期保有は固定年数の条件ではなく、売却までの期間が想定より長引いても事業継続性と回収余地が残るかを見るための selection principle である。

含み損が出ている場合は、損失確定を急がず長期保有へ切り替える余地を持つ。その間の資産ロックは受け入れる。ただし、この方針は短期 thesis が外れた場合に損失を無視するためのものではない。長期保有へ切り替える余地がある銘柄だけを最初から選び、資本毀損・機会損失・thesis 破綻のリスクを下げるための selection principle である。

Long-hold fallback は stop loss、invalidation、kill switch、事業継続前提の毀損を上書きしない。長期保有へ切り替えるのは、短期の価格回復 timing を逃しただけで、事業継続性、cash flow、balance sheet、thesis の中核が維持されている場合に限る。

資産ロック中にも収益が見込めるため、配当、自己株買い、安定した shareholder return がある銘柄は優先する。ただし、配当のないお買い得銘柄でも、短期リターンの可能性と payoff が十分に大きく、balance sheet / cash flow の耐久性が確認できる場合は、リスクを取って採用してよい。

## AI long-term structural impact principle

AI は長期では産業規模、需要構造、コスト構造、競争優位、顧客 capex、disruption risk を変え得る構造テーマとして扱う。Baibai-Loop では、AI 影響を短期のテーマ買いではなく、long-hold fallback の質を評価する strategic lens として確認する。

AI が長期追い風になり得る場合は、下落時に長期保有へ切り替える選択肢の期待値を高める可能性がある。一方で、AI による既存事業の disruption、顧客投資循環の鈍化、valuation 過熱、競争優位の毀損は long-hold fallback を弱める要因として扱う。

AI 期待は単独の採用根拠、position sizing 根拠、macro context fit、validator-visible rule にはしない。採用判断は valuation、cash flow、balance sheet、catalyst、競争優位、資本配分、決算鮮度と合わせて行う。
