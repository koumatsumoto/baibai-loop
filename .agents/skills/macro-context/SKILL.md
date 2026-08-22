---
name: macro-context
description: 人間の判断に必要な full-depth macro context を新規評価して発行する。機械 reading の確認だけなら使わない。
---

# Macro Context

レポートは人間判断を起点に必要時だけ作る。書く場合は常に [`macro.md`](../../../docs/reference/macro.md) の full depth を満たす。reading の変化確認だけなら `baibai-engine macro reading --asof <営業日>` を読む。

## 手順

1. **入力を固定する**

   `ops-maintenance` skill に従い market / runs / macro を pull して market を hydrate する。application DB は pull しない。head は ID と as-of、trigger は前回結論を含まない投影結果だけを読む。

2. **前回分析から隔離して現在を評価する**

   今回の core、synthesis、scenario を確定するまで、前回 context の本文、scorecard 条件、確率、issue / report / UI 上の同内容を開かない。事前に触れた場合は独立性を回復できないため、その context の author を汚染のない別 session へ渡す。

3. **data health と一次情報を揃える**

   `macro refresh <series...> --start <date> --end <asof>` の後、全 series の reading を読む。stale、insufficient history、flag、極値、次回公表を解釈より先に確認する。8レンズで force 仮説を立て、各仮説の支持と反証を一次 source で確認する。source tier、取得失敗時の代替、単位・公表日・取得日は [`data-sources.md`](../../../docs/reference/data-sources.md) に従う。

4. **current report を書く**

   同じ as-of の `screening market-snapshot` を machine snapshot として引用する。macro reference の固定順 core、3 scenario、monitoring、synthesis、connection を満たす。dominant force は2つ以上の伝達チャネルを一次情報と series で実証し、counter-evidence を持たせる。connection は core から導出し、個別 thesis を直接変更せず、識別可能な research hint、sizing caution、bargain topography、estimate caveat を渡す。

5. **前回 scorecard を接続する**

   current の評価を確定した後だけ、前回 context と `macro context scorecard` を開く。machine snapshot をそのまま input に束縛し、成立実績と確率の整合、消滅・demote した force を記録する。今回の結論を前回へ寄せない。

6. **反証する**

   publish check の前に [`anti-patterns.md`](../../../docs/anti-patterns.md) の macro 該当項目と [`macro.md`](../../../docs/reference/macro.md) の深度契約を通す。特に qualifier の下流保持、real / nominal 等の量基準、latest source、scenario 算術、機械的 monitoring、fact / judgment 分離を照合する。

   author と別 role が draft と引用 source だけを inputs → facts → judgments → synthesis / summary → connection の順で読む。未確認 qualifier、量の基準、percentile の窓、消滅・demote した force、`usd_jpy` と原則 `jp.10y` の standing exposure を反証する。構造 validation を semantic review の代わりにしない。修正後は影響箇所を再 review し、2巡で収束しなければ publish せず人間へ上げる。

7. **発行する**

   indicator input は scaffold helper で生成する。反復中は `macro context publish <draft> --check`、確定後は確認済み head を `--expected-head` に渡して publish する。cloud 反映は `ops-maintenance` skill に従う。

## 禁止・停止条件

- macro から売買時期、現金比率、個別 sizing、candidate hard gate、機械 ranking の変更を出さない。
- 確率を統計的 edge や自動 sizing に使わない。
- stale / unresolved source を正常値へ補完しない。
- 前回分析への事前接触、一次 source と判断の矛盾、独立 review 未完了では publish しない。

## 正本

- report schema、深度、分析レンズ、review: [`macro.md`](../../../docs/reference/macro.md)
- source tier と取得失敗: [`data-sources.md`](../../../docs/reference/data-sources.md)
- fact / analysis 境界: [`doctrine.md`](../../../docs/doctrine.md)
