# Candidate Discovery post-merge rollout

価値tier: T1 — Candidate Discoveryのcurrent methodを実データで稼働させ、Review SetからResearch判断までをcloud read modelへ一貫して反映する。

## Rollout boundary

- 観測日: 2026-08-31 JST
- final code: `c17d931bb7c0023ba955cbd8aa459ef0595b4655`
- merged PR: [#1137](https://github.com/koumatsumoto/baibai-loop/pull/1137)、
  [#1140](https://github.com/koumatsumoto/baibai-loop/pull/1140)、
  [#1142](https://github.com/koumatsumoto/baibai-loop/pull/1142)、
  [#1143](https://github.com/koumatsumoto/baibai-loop/pull/1143)、
  [#1145](https://github.com/koumatsumoto/baibai-loop/pull/1145)
- application DB: v19、`integrity_check=ok`、foreign key violation 0
- runs DB: v5、screening run 2 / Security Analysis 7,410 / Review Set 2、`integrity_check=ok`、foreign key violation 0

PR #1143でJPX required flag、method identity、Review Set analysis validation、Research Workspace語彙を修正した。
PR #1145ではcurrent screening viewが表示中のReview Setに属する最新Research Triageだけを選び、そのTriageの
assessmentだけを表示するようにした。これにより、新しいReview Setへ旧Triageや旧3836 assessmentが混入しない。

## Main operational run

- workflow: [cloud-daily-batch run 33319752773](https://github.com/koumatsumoto/baibai-loop/actions/runs/33319752773)
- head: `d30a53b65b57d35258244821ac2a49082d39e9c2`
- input: `asof=2026-08-28`
- result: success、28分30秒
- screening run: `run-revision-6fe887f90b5b4868a12df56f802ae47a`
- Review Set: `review-set-20260828-548f88dc3560`、20件
- method hash: `ddac4f669abed4d4e9d35c1a54d93fd6f0a73ef329c00e454191bbd485d0a7f6`

Review Setのmembershipと順序は、修正前の実運用結果から変わらなかった。新しいrunにはrequired JPX flag判定へ
入力されるflagを持つSecurity Analysisが28件あり、修正済みpredicateとmethod identityを実データで通過した。
所要時間の内訳、通常見積り35分、外部timeout 60分、completion-driven wait契約は
[`batch/OPERATIONS.md`](../../../batch/OPERATIONS.md#cloud-daily-batch-の所要時間)へ反映した。

## Review Set and Research

current Review Setの20件は次のとおり。

`7280, 4222, 9130, 6419, 6547, 7888, 2501, 8511, 6986, 9501, 5946, 6629, 9401, 2168, 7279, 5210, 7130, 5019, 5351, 6862`

- Research Triage: `research-triage-20260828-current-method-operational-validation`
- decision: 6419を`research`、残り19件を`skip`
- 古い候補2168: `skip`
- 3836: Review Setに存在せず、対象外
- triage snapshot: source Review Setとrunに一致し、20件すべてmachine snapshotを保持

6419のResearchは一次情報による統合判断まで進め、`reject`とした。評価時終値3,195円に対してFVは
2,623.6841円、base caseの5年CAGRは4.31%、hurdleは8.5%だった。顧客集中と構造的需要減少に関する
独立反証のevidenceが不足しており、buyへ進める根拠はない。同一agentが独立reviewを捏造せず、active operation
`op-20260828-capital-allocation-1`は`research-completed-awaiting-independent-review`で停止した。

## Cloud reflection

current methodへ結び直したResearch Triageをapplication DBへpublishし、次のmaterializeを完了した。

| workflow | head | result | elapsed |
| --- | --- | --- | ---: |
| [33321377589](https://github.com/koumatsumoto/baibai-loop/actions/runs/33321377589) | `d30a53b65b57d35258244821ac2a49082d39e9c2` | success | 10分31秒 |
| [33322508441](https://github.com/koumatsumoto/baibai-loop/actions/runs/33322508441) | `c17d931bb7c0023ba955cbd8aa459ef0595b4655` | success | 9分16秒 |

最終materialize後にR2のserving viewを直接取得し、次を確認した。

- source runは`run-revision-6fe887f90b5b4868a12df56f802ae47a`
- source Review Setは`review-set-20260828-548f88dc3560`
- Research Triageのsource run / Review Setは表示中のものと一致
- 6419が`research`、残り19件が`skip`、unreadable 0
- `capital_allocation_assessments`は空で、旧3836 assessmentを表示しない
- `views/meta.json`の生成時刻は2026-08-31 01:33:24 JST、screening as-ofは2026-08-28、batch eventはmanual

code、machine store、application judgment、serving viewが同じcurrent run / Review Set / Triageを参照しており、
今回のrolloutに必要なcloud反映は完了した。独立review待ちはResearch contract上の停止条件であり、cloud反映漏れではない。
