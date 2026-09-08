# Opportunity coverage pilot: W0 / W1 事前固定

価値tier: T1 — Discovery・Triage・調査枠のどこで研究価値のある企業が落ちるかを、同じ時点の固定標本で区別する。

[#1249](https://github.com/koumatsumoto/baibai-loop/issues/1249) W0 / W1の母集団と抽出を固定する。これは一次資料レビュー前の機械断面であり、Y/N/U、漏れ診断、比較優位をまだ含まない。主担当が本書をcommitした後、#1263 S1で独立判定基準の誤読を点検してからW2へ進む。run、層、seed、標本数は結果を見て変更しない。

## 対象時点とauthority

W0開始時のlatest canonical Triageを`as_of DESC, julianday(published_at) DESC, research_triage_id DESC`で選んだ。対象runはpruneされておらず、全Security Analysisを保存できた。

| 項目 | 固定値 |
| --- | --- |
| as_of | `2026-09-07` |
| run_at | `2026-09-08T08:26:05.287906+09:00` |
| information_cutoff | `2026-09-07T23:59:59.999999+09:00` |
| triage_id | `research-triage-20260907-5a886e2984a8b4f7` |
| review_set_id | `review-set-20260907-a14dfd52d1d9` |
| run_revision_id | `run-revision-7ab9668455a845cb878db12a5f7bdbce` |
| screening_rules_hash | `c525a6c55309450f` |
| method_id | `multi-valuation-v4` |
| method_hash | `3e4865e072f6559c5873072e65f5b418722b5d096988bbb7caa7611c10b67145` |
| nomination_depth | 20 / Approach |
| code | `317ca38c413c89872c36f8d7229eabd48a7f9051` |
| rules | `method/screening/rules/2026-09-03T161939+0900.yaml` |

`information_cutoff`はrun実行時刻とas-ofのJST日末の早い方。翌朝のrun時刻を判断資料の許容期限に使わない。run機械断面と、その入力の公表時刻検証は別である。主ラベルにはcutoff以前の公表を確認できる一次資料だけを使い、同日で重要な公表時刻を確定できなければ未知とする。

当日masterは4,434行、当日日足も同じ4,434ticker。master coverageは`ok`、4,434件で、取得は2026-09-07 22:44:49 JST。JPX required sourcesは4件全て同日22:45:20 JSTのsnapshotが存在する。EDINETなど一部coverageの取得時刻は翌朝である。取得時刻を公表時刻と読み替えず、W2で当時の一次資料を確認する。全Analysisはsource runが保持した原文を使い、現在の財務から過去runを再生成していない。

## W0: 母集団と層

当日master、当日日足、全Analysisのticker unionは4,434。instrument / marketのpolicy除外は`screening.universe.build_universe`が返す`POLICY_EXCLUSION_REASONS`だけを使う。common eligibilityは`CommonEligibilityRules.matches`、native eligibilityと全順序は`screening.discovery.review_set`のownerで再構成した。独自の簡略投資filterは使わない。

明確なpolicy不合格をXへ先に分け、残りをcommon eligibility通過かつAnalysisありのU、未成立のDへ分けた。Xの副次的欠損をDへ重複計上しない。native入力欠損による全Approach不成立はD0に残す。

| 集合 / 層 | N | 意味 |
| --- | ---: | --- |
| U | 2,400 | common eligibility通過、Analysisあり |
| D | 149 | 全件がAnalysisあり、market_cap_oku欠損で共通判定未成立 |
| X | 1,885 | 既存policyで明確に除外 |
| A | 77 | Review Set内、Triage research |
| B | 0 | Review Set内、Triage skip |
| C | 2,275 | Review Set外、native eligibilityが1つ以上成立 |
| D0 | 48 | U内、native eligibilityが全て不成立 |

`A+B+C+D0=2,400`、`U+D+X=4,434`、Review Set=`A∪B`=77件、Triage全件exactly once、research priority=`1..77`を検証した。4つのtop20のexact unionは77件で、canonical Review Setの全entry・全Nomination・analysis・diagnosticsをowner再計算と照合済み。source run / Review Set / Triageのrules hashとmethod hashも一致する。

Bが空のため、この対象日ではTriage skipの取りこぼしを測定できない。他層から補充せず、skip品質を良好とも不良とも判定しない。

| Approach | native eligible数 | Nomination数 |
| --- | ---: | ---: |
| current-earnings-power | 2,308 | 20 |
| normalized-earnings-power | 1,880 | 20 |
| asset-value | 1,086 | 20 |
| reinvestment-value | 105 | 20 |

Xの理由別件数は重複を許す。market cap下限未満1,118、listing span下限未満26、JPX flag 31、market範囲外728、sector分類範囲外542。したがって理由の合計をXの母数としない。全Analysis 3,705件とmasterの差729件は全てownerのstructural policy除外。XのうちAnalysisがある21件にcommon fact欠損が併存し、残る729件はAnalysisなしとして別記録した。Xは主標本の母数外であり、Xに研究価値のある企業が無いとは主張しない。

当日master/価格に存在しながらpolicy除外で説明できないAnalysis欠落は0件。これは保存されている当日masterに対する完全性であり、提供元master自体の外側まで市場全体を保証するものではない。historical Candidate Discovery fidelityの過去JPX不足を今回の当日完全性で解消したことにせず、[既存検証記録](../../2026-09-03-nomination-union-triage-validation.md)の制約を維持する。

## W1: 結果から独立した抽出

層内でUTF-8文字列`baibai-1249-v2|<run_revision_id>|<ticker>`のSHA-256 hex昇順、同値時ticker昇順。canonical ticker表記をそのまま用い、A/B/C/D0は各最大8、Dは最大4、空層は0として他層から補充しない。

| 層 | N | n | 抽出順のticker |
| --- | ---: | ---: | --- |
| A | 77 | 8 | 8887, 9658, 6675, 6199, 4439, 3660, 7279, 9145 |
| B | 0 | 0 | なし |
| C | 2,275 | 8 | 6752, 3962, 6463, 2432, 4970, 7956, 3197, 4960 |
| D0 | 48 | 8 | 4499, 4376, 4812, 2160, 8766, 6740, 4575, 4588 |
| D | 149 | 4 | 7383, 6579, 350A, 4890 |

主標本24社、欠損診断Dは4社。主標本のY/N/UにDを合算しない。層内同数なので主標本のY単純合計を市場recallにしない。#1249 W3の層別重みと未知感度幅を使う場合もsampling errorの信頼区間とは呼ばない。8件で漏れを確認しなくても「漏れなし」や希少機会への十分な検出力は主張しない。

Oは原Triageのresearchをpriority昇順に読んだ`6419, 4231, 2415, 2221`。4社とも主標本外であり、今回の一次資料確認対象は合計32社になる。Oは実際に人間が選んだResearch Setと区別し、主標本の率へ加算しない。

canonical ledgerの最終event時点は2026-09-04だが、これはeventの無い後日のreplayを妨げない。`position.ledger.replay_events_through`にcanonical報告済みeventを時刻・同時刻順で渡し、cutoffまでの保有・active予約を価格なしで再構成した。**Oと後続のC*の両方で同じ保有・予約tickerを除く。** exact除外集合とevent原文はprivateに保存する。参考private ledgerの価格観測日は資本eventの日付と区別し、現在時価をreplayへ入力しない。未報告broker状態を推定せず、通常の報告済みledgerの範囲で解決した。資本条件は主標本のラベルや率を変更しない。

## 後続判定への受け渡し

本書、`population.json`、`native-orders.json`、`draws.json`、原Triage/Thesis/CAAは所属を知る集計担当だけが読む。W2の独立review担当へは所属・原順位・Nomination・E[r]の推奨的表現・原判断・未来株価/業績を除いた資料を別途用意する。case IDの順序も全対象の同じhash順にした。tickerまで匿名化したとは主張しない。

ここでは一次資料レビュー、Y/N/U、W3原因判定、C*選定、future return閲覧を行っていない。W2のrubricは#1249 §4を出発点とし、#1263 S1で誤読を点検してから主が固定する。1人目の結論と所属を知らない独立contextの2人目が同じ一次資料を検証する。既存研究が重なる場合もcutoffと資料範囲が同じ部分だけ再利用する。

W4では主標本YからOを除き、出自を伏せて最大4社のC*を深掘り前に固定する。O∪C*の最大8社から合計最大4社を比較する。W0/W1でW4の経済判断や結論を先取りしない。実測できる資料取得数・所要時間・AI利用量を後段で記録し、不明な費用を0にしない。

## 保存物と再実行

公開成果物は本書。canonical原文、全Analysis、資本時点、全母集団と所属対応、抽出hashと順序はGit管理外の`.cache/studies/opportunity-coverage/`へ保存した。private SQL snapshotは原payload文字列も保持する。正式publish、DB mutation、cloud dispatch、R2 push、broker操作は行わない。

実行は対象worktreeのengine ownerを`PYTHONPATH`で明示し、全storeをSQLite `mode=ro`、`PRAGMA query_only=ON`、read transactionで読む。主repoの`.venv`は実行環境だけに用いた。

```bash
cd /tmp/baibai-1249-study
/home/kou/baibai-loop/.venv/bin/baibai-engine screening --help
/home/kou/baibai-loop/.venv/bin/baibai-engine research --help
PYTHONPATH=/tmp/baibai-1249-study/engine/src \
  /home/kou/baibai-loop/.venv/bin/python \
  /home/kou/baibai-loop/.cache/studies/opportunity-coverage/prepare.py
python3 /home/kou/baibai-loop/.cache/studies/opportunity-coverage/verify.py
```

`prepare.py`はこのstudyだけのprivate抽出手順。運用CLI、stable module、schema、production testを追加しない。latest headが変わった再実行は冒頭のexact ID確認で停止する。固定後の照合は保存済み断面を読み、対象日を自動変更しない。全読み取りqueryと実引数は`queries.json`に保存した。主要queryは次のとおり。

```sql
-- application; parameters=[]
SELECT * FROM research_triage ORDER BY as_of DESC, julianday(published_at) DESC, research_triage_id DESC LIMIT 1;
-- runs; parameters=["run-revision-7ab9668455a845cb878db12a5f7bdbce"]
SELECT * FROM screening_run WHERE run_revision_id = ?;
-- runs; parameters=["run-revision-7ab9668455a845cb878db12a5f7bdbce"]
SELECT * FROM security_analysis WHERE run_revision_id = ? ORDER BY ordinal;
-- runs; parameters=["review-set-20260907-a14dfd52d1d9"]
SELECT * FROM review_set WHERE review_set_id = ?;
-- market; parameters=["2026-09-07"]
SELECT * FROM jquants_master_snapshots WHERE snapshot_date = ? ORDER BY ticker;
-- market; parameters=["2026-09-07"]
SELECT * FROM jquants_daily_bars WHERE traded_at = ? ORDER BY ticker;
-- market; parameters=["2023-05-26", "2026-09-07"]
SELECT ticker, COUNT(*) AS bars, MIN(traded_at) AS first_date, MAX(traded_at) AS last_date FROM jquants_daily_bars WHERE traded_at BETWEEN ? AND ? AND close IS NOT NULL GROUP BY ticker ORDER BY ticker;
-- market; parameters=["2026-09-07", "2026-09-07"]
SELECT * FROM source_coverage WHERE coverage_start <= ? AND coverage_end >= ? ORDER BY source, coverage_key;
-- market; parameters=["2026-09-07"]
SELECT * FROM jpx_regulation_sources WHERE asof_date = ?;
-- market; parameters=["2026-09-07"]
SELECT * FROM jpx_regulation_flags WHERE asof_date = ?;
-- application; parameters=[]
SELECT as_of FROM ledger_meta WHERE singleton = 1;
-- application; parameters=[]
SELECT MIN(occurred_at) AS first_event, MAX(occurred_at) AS last_event, COUNT(*) AS event_count FROM ledger_event;
-- application; parameters=[]
SELECT payload FROM ledger_meta WHERE singleton = 1;
-- application; parameters=[]
SELECT payload FROM ledger_event ORDER BY occurred_at ASC, same_instant_order ASC, append_seq ASC;
```

保存物のSHA-256は`sha256.json`に記録した。主な固定値は以下。数値/所属/抽出結果は保存JSONを再読して、相互排他、和集合、hash順のprefix、Oの原priority順、32個のcase IDとtickerの一意性を`verify.py`で別途検証し、全項目PASSを確認した。

| private file | SHA-256 |
| --- | --- |
| prepare.py | `fba0d768aca7b576133a1d8587f64be45d650c420aea8d655a88502644f2c1c2` |
| verify.py | `1148bc4c8f86e6bfba21a0bb6ea277e1fe9b1f8baabb530a75f852169e192d40` |
| run.json | `e1077b84a7f7e37e609ed4fc328c304b8d918180ca5c928fc2cc9a0330a02572` |
| security-analyses.json | `ebc8faed5b8984eb1bba17a82e2aae7720da3d6d611e30079c971e9e052916d5` |
| review-set.json | `3d8ffcf26da4c5a0ea265539baf90636bf34a4cf27404f19c2fb3af805fcc44f` |
| triage.json | `335eb18b35cfb0d49427861ee92bd0fb2fcb4126b4bfe917bec553689aad13e7` |
| population.json | `c0998bfd6ea92725ae1f811d51c3aefe27f1510e94d4252e3cd270dd9313d040` |
| draws.json | `ccd64a37a65819e76a0c474eba5dd533402cce89b3947d37bf8203daebb27457` |
| case-map.json | `bfcd87f0f7a890a49253804e025f351c72427393ca4340024bedbf0ecf114aa9` |
| capital-scope.json | `4eef5cb122a6df5128bac88d2ab2dc6e2b1c1a19fe4e4504b984ea01f3cb72f4` |
| metadata.json | `325a486de914e3c37c4844d2f11546ee4ec13ae8024924710e33a7f5db688be1` |
| queries.json | `e2232b2e3bb5e28388b69e3c1722971c0dbfe63222a7d34a80ff56b8c4ff8f5f` |

作業前にAP-01/02/03/04/05/07/09/12を対象として確認した。commit / PR前は同じ観点で、primary sourceと機械値の区別、時点、母数、未知の扱い、canonical read-onlyを再確認する。標本抽出を承認済みの運用判断や本番の手法改善へ転用しない。

## S1後・W2判定前のrubric固定

2026-09-08 20:21:28 JST、#1263 S1のblind初回と原4件照合を完了。原判断に新規confirmed_errorは見つからず、企業価値仮説を作れることと現価格で買えることを分ける定義を維持した。原4社の独立Research価値はYだが、これは本標本の判定担当へ参考解答として渡さない。

W2はY=具体的な価値仮説と、答えが資本判断を変える調査質問が一次根拠から作れる、N=関連事実を十分確認して経路を否定、U=研究価値の判定を妨げる重要欠損・時点矛盾・根拠ある不一致。低PER、赤字、無配、価格条件外、次期決算待ち、将来推定の幅だけをラベルに変換しない。会社FVを一点で完成させること、12か月内の換金/還元確約、買える価格はYの必須要件ではない。重大な未確認事項を将来幅という名で無視もしない。

`blind-cases.json`はcase ID・ticker・会社名・cutoff・原価格だけの32件、SHA-256=`cea025d64735d56d1ec471cdeb72f380005448e8841127836d269653d4cd5be4`。独立2contextへ同じ最新短信と補足一次資料を渡す。初回ラベルは相互開示せず保存し、不一致は一次資料の事実補正か根拠ある判断差かを主が区別する。重要な不一致が残れば最終Uで、解決のため所属・future returnを利用しない。PDF取得は先行したが、判定は本固定後から開始する。
