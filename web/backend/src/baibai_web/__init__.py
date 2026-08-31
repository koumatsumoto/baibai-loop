"""Read-only application composition surface for `baibai-web`.

Read-only app invariants:

`baibai-web` は `127.0.0.1` にだけbindし、write endpoint、migration、external network clientを持
たない。application DB / run store / macro storeをSQLite read-only modeで開く。
UIの面は8つで、4タブ (`/` Dashboard、`/macro` Macro、`/stocks` Stocks、`/tasks` Tasks) と
タブなし詳細 (`/macro/reports/:contextId` Macro report、`/research-triage` ResearchTriage、
`/stocks/capital-allocation-assessments/:capitalAllocationAssessmentId`
Capital Allocation Assessment、`/securities/:ticker` Security detail)
である。ヘッダーの歯車 menu は GitHub Actions の run 一覧へ外部 link する。assessment全state、
Tasksでoperation active/completed、Dashboardでportfolio outcomeをquery-only viewとして表示する。
Dashboardは前営業日の機械実行との差分 (候補プールの出入り、機械E[r]の変化、
FVに達した保有) を観測として1区画に出し、Macroの現在局面へリンクする。
判定・推奨は持たず、答えられなかった区分を明示して空欄と未計測を区別する。
Macroは `/api/macro` の最新L3 Contextによる現在局面を入口にし、その後へ全登録系列の現在読み値を
`web/config/macro-panel.yaml`の7 groupで配する。chart historyは行を開いた系列だけ
`/api/macro/series/:seriesId`で読む。Stocksは深掘り
research_triageと機械screeningのCandidatesを表示する。ResearchTriage は
`reports/published/er-level-calibration-latest.yaml` が有効な間だけ、候補 E[r] の historical
quintile と独立した要求利回りhurdle以上帯について、実現 total-return の中央値・下方分位・trap率
を文脈表示する。Candidatesはrun storeまたはクラウドの31日履歴から日付を選べる。
`/api/meta`はscreening / macro / application DBのas-of鮮度と最新データ時刻をstore内timestampから
返し (file mtimeに依存しない)、共通ヘッダーはUI build時刻と最新データ時刻だけを表示する。
"""
