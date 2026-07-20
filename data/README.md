# data

ローカルで必要な永続データを置くディレクトリです。`records/` は履歴成果物、
`.cache/` は削除可能な一時 cache、`data/` は実行に必要な local store です。

実データファイルは git 管理しません。

## Store classification

| store | classification | rebuild command |
| --- | --- | --- |
| `app/baibai.sqlite` | canonical application DB | —（`db backup` の出力から file-level で復元） |
| `screening/market.sqlite` | rebuildable L1 | `uv run baibai-engine screening bootstrap-cache --asof YYYY-MM-DD` と `uv run baibai-engine screening extract-edinet-metrics --asof YYYY-MM-DD` |
| `screening/runs.sqlite` | rebuildable L2 | market coverage を確認して `uv run baibai-engine screening run --asof YYYY-MM-DD`。容量に応じて `uv run baibai-engine screening prune --keep 3` |
| `screening/calibration/` | rebuildable L2 calibration store | `uv run baibai-engine screening calibration-build --start YYYY-MM-DD --end YYYY-MM-DD --force` |
| `indicators/macro.sqlite` | rebuildable L1 | `uv run baibai-engine macro import-manual` と、provider 系列ごとの `uv run baibai-engine macro refresh <series-id> --start YYYY-MM-DD --end YYYY-MM-DD` |

application DB の backup は `uv run baibai-engine db backup` で作成します。rebuildable store は provider と Git 管理の method / config から再生成し、backup 対象に含めません。
