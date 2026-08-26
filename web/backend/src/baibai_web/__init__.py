"""Read-only application composition surface for `baibai-web`.

Read-only app invariants:

`baibai-web` は `127.0.0.1` にだけbindし、write endpoint、migration、external network clientを持
たない。application DB / run store / macro storeをSQLite read-only modeで開く。
UIの面は8つで、3タブ (`/` Dashboard、`/macro` Macro、`/stocks` Stocks)、
タブなし詳細 (`/macro/reports/:contextId` Macro report、`/stocks/shortlist` Shortlist、
`/stocks/assessments/:assessmentId` Bargain assessment、`/securities/:ticker` Security detail)、
ヘッダーの歯車から入る運用状態画面 (`/system` System) である。proposal全state、
operation active/completed、portfolio outcomeをquery-only viewで表示する。
Dashboardは前営業日の機械実行との差分 (候補プールの出入り、機械E[r]の変化、
FVに達した保有、macro readingの注記と分布の端の遷移) を観測として1区画に出す。
判定・推奨は持たず、答えられなかった区分を明示して空欄と未計測を区別する。
Macroは経済分析レポートと、全登録系列を`web/config/macro-panel.yaml`の7 groupへ配した1つのマクロ
経済指標一覧 (`/api/macro`のチャートと`/api/macro/reading`の記述統計を`series_id`でjoinし、
取得失敗・stale・履歴不足・分布の端の件数を上部の要約カードへ畳む)、Stocksは深掘りshortlistと機
械screeningのCandidatesを表示する。Shortlist は
`reports/published/er-level-calibration-latest.yaml` が有効な間だけ、候補 E[r] の historical
quintile と独立した要求利回りhurdle以上帯について、実現 total-return の中央値・下方分位・trap率
を文脈表示する。Candidatesはrun storeまたはクラウドの31日履歴から日付を選べる。
`/api/meta`はscreening / macro / application DBのas-of鮮度と最新データ時刻をstore内timestampから
返し (file mtimeに依存しない)、共通ヘッダーはUI build時刻と最新データ時刻だけを表示する。
"""
