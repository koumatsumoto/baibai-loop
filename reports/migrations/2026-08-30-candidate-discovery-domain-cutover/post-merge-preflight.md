# Candidate Discovery post-merge preflight

価値tier: T2 — Candidate Discoveryのmethod、canonical store、serving viewを同じcurrent architectureへ収束する。

## 観測境界

- 観測日: 2026-08-31 JST
- code: `1757bdae363dda7b814385eb8b2a5197e62b4900`
- application DB: v19、`integrity_check=ok`、foreign key violation 0
- runs DB: v5、`integrity_check=ok`、foreign key violation 0
- cloud application / machine bundleを正本のtransfer commandでlocalへ取得してから観測した
- production storeの手書き更新、旧schema互換、過去SelectionからのNomination捏造は行っていない

## Canonical state

| surface | observed state |
| --- | --- |
| application | task 97 / operation 18 / active operation 1 / ledger event 25 / thesis 24 / Research Triage 14 |
| active operation | `op-20260828-capital-allocation-1` / `capital-allocation` / checkpoint `research-completed-awaiting-independent-review` |
| runs | screening run 1 / Security Analysis 3,705 / Review Set 1 |
| current Review Set | `review-set-20260828-e10f2903278e` / source `run-revision-c957c9617c4d4c4b94be423b4c5d18d4` / 20 entries |
| application SHA-256 | `b289664d56708716be7bb4169ece7f77027645e3784f6c39f13712cdbc5e4c2c` |
| runs SHA-256 | `52033ceea635b676ecc2d50154f44f2899e1d1eb32e95d8b2eba7ac6d52aaecf` |

active operationは失敗ではない。6419のResearchは`reject`まで完了したが、同一agentが独立reviewを捏造せず、
独立review待ちとしてcheckpointした。application schema cutoverはすでに完了しているため、active operationを理由に
旧schema migrationを再実行しない。

## Serving rollout

- L1 release: `20260830T143346Z-c66f7c30-d42169a25590`
- manifest SHA-256: `c1ca683b6992fc0a5a6547d937f164e86366a1c71f8ee32d5ea35c56cc064d1c`
- cloud materialize: [run 33318089095](https://github.com/koumatsumoto/baibai-loop/actions/runs/33318089095)
- workflow head: `1757bdae363dda7b814385eb8b2a5197e62b4900`
- result: success
- serving verification: source run、Review Set 20件、corrected Research Triageの6419 `research` / 残り19件 `skip`、unreadable 0

cloud materializeの実測10分20秒と内訳、通常見積り12分、外部timeout 20分、completion-driven waitは
[`batch/OPERATIONS.md`](../../../batch/OPERATIONS.md#cloud-materialize-の所要時間)へ反映した。

## Corrected-method actual-data replay

同じ2026-08-28 Security Analysis 3,705件を独立copyへ再生し、required JPX flag setをsole eligibility predicateへ渡した。

| metric | result |
| --- | ---: |
| common eligible | 1,592 |
| nomination | Current 20 / Normalized 20 / Asset 20 / Reinvestment 20 |
| unique Candidate | 73 |
| Review Set | 20 |
| represented | Current 8 / Normalized 8 / Asset 5 / Reinvestment 6 |
| unfilled target | 全approach 0 |

- corrected method hash: `ddac4f669abed4d4e9d35c1a54d93fd6f0a73ef329c00e454191bbd485d0a7f6`
- replay Review Set: `review-set-20260828-674f2ffab38c`
- current production method hash: `ba90c1a6b3f92fcc5eb39bda6fb58dcbe087ffbac876e584c368ff8e8f434b81`
- membership / order difference: 0
- observed rows with any JPX flag: 0

membershipが不変だったのは今回のinputにJPX flag rowが無かったためであり、修正を不要とは判定しない。contract testでは
required flagだけを除外し、未知のnon-required flagをeligibleのまま保つ。required flag集合はmethod hashへ含めた。

## Method identity

- historical `2026-07-06T000000+0900.yaml` SHA-256: `dc595e766d011c4d4b7dcdbe251d41965c3ba417e21c4b8dfa3a591f99ba3458`
- current `2026-08-30T215359+0900.yaml` SHA-256: `eb53e9312df4ef4d0262a5593b19a8611de82be77e968f61ab2c74975ae8a447`
- `revisions.sha256`とcontract testがdated revisionのin-place変更と未登録fileを拒否する

## Remaining attended verification

修正PRをmainへmergeした後、target mainの`cloud-daily-batch`を1回だけ実行する。run完了通知まで待ち、成功後に
cloud machine bundleを正本transfer commandで取得して、v5、integrity、Review Set source run、method hash、20 entries、
serving refsを再確認する。この結果はIssue #1138と#1136へ記録する。
