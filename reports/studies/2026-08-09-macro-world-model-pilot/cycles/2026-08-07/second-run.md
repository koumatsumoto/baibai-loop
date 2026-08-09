# Second-run checks

同じ operation を再度なぞるときの create-only、hash、CAS の境界を実コマンドで確認する。

| check | result | evidence |
| --- | --- | --- |
| snapshot output overwrite | expected reject | exit 1: `refusing to overwrite existing output: .../evidence-snapshot.json` |
| report output overwrite | expected reject | exit 1: `refusing to overwrite existing output: .../one-page.md` |
| snapshot hash, same stable inputs | pass | 連続 2 回とも `5542ad596c3dbe832a98b0b9758983c602ebfc1d06e077f4eae59c5323823cc2` |
| stale expected-head CAS | expected reject, no mutation | exit 1: expected predecessor / actual new head。拒否後 head は `macro-context-2026-08-07-labor-capex-divergence` |

初回 blind snapshot の hash `ec118095…` と、operation 後に手順を先頭から再実行した hash `5542ad5…` は一致しない。原因は非決定性ではなく、freeze 後の scorecard settle 証明で 8 月 1–7 日の 8 series を明示 refresh し、L1 store の入力集合が増えたことである。refresh 後に同じ store / as_of / coverage config で連続生成した 2 snapshot は一致する。

したがって canonical hash は「同一入力」に対して冪等である一方、full operation は scorecard 証明のため L1 を更新し得る。再実行時は初回 freeze hash との一致を要求せず、snapshot に記録された file hash と store input の時点を区別する。これは cycle 2 の pass ordering で、scorecard proof refresh に使う series を blind snapshot 前にも取得可能か検討する申し送りとする。prior 本文は freeze 前に開かない制約を維持する。
