# Claude Code Instructions

このリポジトリで作業する際の手順は [`AGENTS.md`](./AGENTS.md) を読むこと。AI エージェント全般の規約はそこに集約する。

## 優先度ルール（明示）

- **後方互換性は通常気にしない**。schema 変更・field rename・CLI subcommand 削除・records front matter の breaking change を躊躇しない。互換性レイヤー、deprecation 警告、migration shim を加える前に、まず古い実装を捨てる選択肢を取る。1 人運用 + git history が retroactive 復元の安全網。
- **監査用ファイル / 監査専用 logic は通常作らない**。validator / forward 計測 / replay の output が一次資料として残るので、追加の audit trail / lineage / provenance を別途設けない。AGENTS.md AP-08 / philosophy 柱 5 の「計測経路のない機能は追加しない」を強く適用する。
  - ✓ 削除可: 1 度も書かれていない schema field、template だけの records、forward telemetry に置換可能な review/retro 系
  - ✗ 残す: 実取引が絡む order/fill audit、ledger correction event、法務・規制・税務で必須の記録、schema migration log
  - △ 迷ったら: 削除前に `rg <symbol>` で実利用を確認し、「forward 計測経路を 1 行で説明できるか」を self-check。説明できなければ削除候補。判断に迷ったら短く確認する。
- 機能を増やすより既存機能を整理・削除する方を優先する。削除根拠は計測・実利用の確認（`rg` での参照有無、運用での使用実績）で示す。
