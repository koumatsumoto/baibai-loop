---
name: macro-context
description: 人間の判断に必要な full-depth の macro context を新規評価して発行する。機械 reading の確認だけなら使わない。
---

# Macro Context

レポートは、人間の判断に必要なときだけ作り、定例更新はしない。作る場合は常に [`macro.md`](../../../docs/reference/macro.md) の full depth を満たす。reading の変化だけを確認する場合は `baibai-engine macro reading --asof <営業日>` を読む。

## 手順

1. **入力を固定する**

   `ops-maintenance` skill に従い market / runs / macro を pull し、market を hydrate する。application DB は pull しない。head は ID と as-of だけを読む。

2. **前回分析から隔離して現在を評価する**

   今回の core、synthesis、scenario を確定するまで、前回 context の本文、scorecard 条件、確率、および同じ内容を載せる issue / report / UI を開かない。事前に触れた場合は独立性を回復できないため、その context の執筆を汚染のない別 session へ引き渡す。

3. **データの健全性と一次情報を揃える**

   pull 済みの macro store は daily batch が全登録 series を refresh した成果である。まず全 series の `macro reading --asof <asof>` を読み、stale、insufficient history、flag、極値、次回公表を解釈より先に確認する。stale、取得失敗、または結論を左右する最新公表だけを `macro refresh <series...> --start <date> --end <asof>` で再取得し、再度 reading を確認する。context 作成のたびに全 series を無条件 refresh しない。8レンズから force 仮説を立て、各仮説を支持する一次 source と反証する一次 source の両方を確認する。source tier、取得失敗時の代替、単位、公表日、取得日は [`data-sources.md`](../../../docs/reference/data-sources.md) に従う。

4. **今回の report を書く**

   同じ as-of の `screening market-snapshot` を machine snapshot として引用する。[`macro.md`](../../../docs/reference/macro.md) が定める固定順の core、3 scenario、monitoring、synthesis、connection を満たす。dominant force は、2つ以上の伝達チャネルを一次情報と series で実証し、counter-evidence を持たせる。connection は core から導出し、個別 thesis を直接変更せず、識別可能な research hint、sizing caution、bargain topography、estimate caveat を渡す。

5. **前回 scorecard を接続する**

   今回の評価を確定した後だけ、前回 context と `macro context scorecard` を開く。machine snapshot をそのまま input に束縛し、成立実績と確率の整合、消滅または demote した force を記録する。今回の結論を前回へ寄せない。

6. **反証する**

   publish check の前に [`anti-patterns.md`](../../../docs/anti-patterns.md) の macro 該当項目と [`macro.md`](../../../docs/reference/macro.md) の深度契約を通す。特に、限定表現の下流保持、real / nominal などの量基準、latest source、scenario の算術、monitoring の反証可能性、fact / judgment の分離を照合する。

   author とは別 session の role が、draft と引用 source だけを inputs → facts → judgments → synthesis / summary → connection の順で読む。未確認などの限定表現、量の基準、percentile の窓、消滅または demote した force、`usd_jpy` と原則 `jp.10y` の standing exposure を反証する。構造 validation を semantic review の代わりにしない。修正後は影響箇所を再 review する。2巡目も block なら、既知の指摘を直して `publish --check` まで行ったうえで publish せず人間へ上げる。再開には、人間による draft 承認、または追加の独立 review を行う明示指示が必要である。

7. **発行する**

   indicator input は `baibai_engine.macro.context.scaffold_inputs` で生成する。反復中は `macro context publish <draft> --check`、確定後は確認済み `macro context head` 出力の `context_id` 値だけを `--expected-head` に渡して publish する。head の YAML 出力全体は渡さない。cloud 反映は `ops-maintenance` skill に従う。

## 禁止・停止条件

- macro から売買時期、現金比率、個別 sizing、candidate hard gate、機械 ranking の変更を出さない。
- 確率を統計的 edge や自動 sizing に使わない。
- stale / unresolved source を正常値へ補完しない。
- 前回分析への事前接触、一次 source と判断の矛盾、独立 review の未完了がある場合は publish しない。

## 正本

- report schema、深度、分析レンズ、review: [`macro.md`](../../../docs/reference/macro.md)
- source tier と取得失敗: [`data-sources.md`](../../../docs/reference/data-sources.md)
- fact / analysis 境界: [`doctrine.md`](../../../docs/doctrine.md)
