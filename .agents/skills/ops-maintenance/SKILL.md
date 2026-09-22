---
name: ops-maintenance
description: batch・store・servingの操作を選び、既存runbookに従って反映・復旧・定期作業を行う。
---

# Ops Maintenance

## 操作を選ぶ

| 依頼・状態 | 操作の正本 |
| --- | --- |
| machine storeの取得 | [クラウド正本の取得](../../../batch/OPERATIONS.md#クラウド正本をローカルへ取得する) |
| machine storeの反映 | [ローカルからクラウドを更新する](../../../batch/OPERATIONS.md#ローカルからクラウドを更新する) |
| application DB・viewの公開 / application DBの復元 | [application反映](../../../batch/OPERATIONS.md#application-db-を反映する) / [復元](../../../batch/OPERATIONS.md#application-db-を復元する) |
| credential rotation | [Password rotation](../../../batch/OPERATIONS.md#password-rotation) |
| local Triageの失敗・再実行 | [Research Triage](../research-triage/SKILL.md) |
| batchの失敗・欠測 | [batch OPERATIONS](../../../batch/OPERATIONS.md)の結果確認・復旧・watchdog |
| 保有の再評価 | [Position Review](../position-review/SKILL.md) |
| 人間からの注文・資金報告 | [Ledger Record](../ledger-record/SKILL.md) |
| 手法の計測・採否 | [Estimate calibration](../../../docs/reference/estimate-calibration.md) |

正本・依存は[architecture](../../../docs/architecture.md#store-authority)に従う。ここに転送command、失敗調査、no-loss条件を複写しない。`research_watch`の原評価比較は保有の売却判断ではなく、必要なら対象holdingのPosition Reviewへ進む。

## 定期maintenance

既存のdated taskがdueになった対象を実行する。

- calibrationは月初に前月の完全月末まで、同じrulesでbuild→evaluateする。全再構築以外で`--force`を使わない。範囲と操作は[較正reference](../../../docs/reference/estimate-calibration.md#store-の再構築)に従う。
- PMI manifestは公表翌週にdry-run→本実行→対象月のrefresh→公表値照合→commitを行う。
- TSE capital policy・JPX delistingは翌月15日以降に既存のrefresh/build commandで更新し、[batch運用](../../../batch/OPERATIONS.md)のlake公開・store反映へ進む。
- lake全履歴監査を定例作業に追加しない。不要になった既存の監査taskは終了し、通常の公開・利用時検証だけを維持する。

注文報告だけを理由に全体計測を起動しない。注文結果の計測が依頼された場合は`tools.experiments.measure_limit_outcomes`の契約を参照する。

## 運用task

運用taskはapplication DBで管理し、同じ期限・種別・対象のopen taskがあれば更新または統合する。実行後は完了を記録し、必要な反復taskだけを次回分として作る。開発変更はGitHub Issueで扱う。
