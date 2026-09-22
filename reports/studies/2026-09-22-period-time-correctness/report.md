# 期間・時点の整合性修正（#1341）

## 比較条件（結果の取得前に固定）

- as-of: 2026-09-18（取得したrun storeの最新営業日）。
- 入力: 2026-09-22に正規pullで取得・hydrateしたmarket.sqliteを、この作業中は更新せず両計算で共有する。
- 固定したmarket.sqliteのSHA-256: `21b656ab5c16bd9dd4065d614e222bee3778b76a20e850772be2db75f143baef`（比較後の照合値。作業中にsourceへのwriteなし）。
- L1 release: `20260922T103754Z-ae92d11f-bf6b53fea226`。
- 旧計算: main `c24bf837a21393b6036e0b6aea0a2f558c793338`のReinvestment実装。
- 新計算: 同じcanonical panel/FinancialSnapshot/Security Analysis経路で得たTTM営業利益を使用する。
- 比較項目: eligible数、sector/market floor、top20、Nomination union、1814、他3 Approach、Security Analysis変更件数。
- correctnessの影響測定であり、将来収益改善や投資判断の根拠にはしない。閾値・depthを調整しない。
- calibrationは保持中の2019-11〜2026-08の82か月を`--force`で全再構築する。

## 固定断面での結果

canonical `calibration.panel.build_panel`が組み立てたSecurity Analysis 3,703件を取得し、旧revisionの`discovery.review_set`と新実装へ同じ行を渡した。旧側では追加fieldだけを除いた。既存`operating_profit`は変更していないため、旧側は累計営業利益、新側は明示TTMを読む。他の入力は同一であり、過去Run・Review Set・判断本文を更新していない。

| 項目 | 修正前 | 修正後 |
| --- | ---: | ---: |
| Reinvestment eligible | 96 | 52 |
| Nomination union | 77 | 76 |
| market operating margin floor | 2.482910% | 8.180483% |
| market capital return floor | 4.418341% | 15.153405% |
| 1814 営業利益率 | 1.880637% | 7.053282% |
| 1814 capital return | 11.988896% | 44.964060% |

1814の`operating_profit_ttm`は7,891,000,000円、`sales_ttm`は111,877,000,000円。最新累計`operating_profit`の2,104,000,000円は保持した。数値は丸めた資料値の手計算だけでなく、固定したsourceからcanonical codeで再計算した。

営業利益率の上昇がそのまま候補採用を意味するわけではない。比較母集団全体のfloorとcapital return順序も変わる。1814は旧top20から外れ、他3 Approachからも選ばれないためunionから外れる。

- top20追加: 1828, 2307, 2819, 3034, 3640, 3916, 4008, 4977, 4994, 5184, 6493, 6547, 6638, 6643, 6932, 7254, 7419, 9029, 9311, 9709
- top20除外: 1814, 2384, 3048, 3321, 3660, 3675, 3843, 4554, 4577, 4631, 4633, 4763, 4996, 5019, 6044, 6199, 7539, 7781, 9418, 9823
- union追加: 1828, 2307, 2819, 3034, 3916, 4008, 4977, 4994, 5184, 6493, 6547, 6638, 6643, 6932, 7254, 7419, 9029, 9311, 9709
- union除外: 1814, 2384, 3048, 3321, 3660, 3675, 3843, 4554, 4577, 4631, 4633, 4763, 4996, 5019, 6044, 6199, 7539, 7781, 9418, 9823

Current Earnings Power、Normalized Earnings Power、Asset Valueのtop20は順位まで一致した。Security Analysis payloadは3,703件で追加fieldにより変わり、うち3,438件が非null、265件はnullである。既存metricsの意味は変えない。sector別floorの前後値・全top20順位は[impact.json](./impact.json)に保存した。

## 修正範囲

- WS1: TTM営業利益を既存ownerで作り、Reinvestmentの利益率・capital returnに使う。calculation revisionをv23、Reinvestment methodをv3へ進め、dated rulesを追加した。
- WS3: benchmarkの観測日に最終価格を持つ銘柄のみで業種集計する。benchmarkが無い場合は保存seriesの最新観測日を使う。20/60本定義とmaster snapshot選択は維持する。
- WS4: Run・Review SetのSQL順序を`julianday`へ統一。MCPのcursorも同じ数値時刻を用い、保存timestamp・schemaは変更しない。
- WS2: Macro履歴の成功・失敗callbackをabort状態でguardし、失敗時は詳細chartと期間captionを表示せず取得失敗を示す。

## 較正評価のscope（再構築結果の取得前に固定）

contextのrequired scopeは、既存ownerが判定する3y/5y両方のintegrity・E[r]必須metricがeligibleな月の全共通集合とする。収益率や候補順位で月を選ばない。空集合ならcontextを生成しない。Candidate Discoveryのfidelityは別に全cohortで計数し、JPX入力不足・未成熟forward等の既存制約を隠さない。この修正をempiricalな閾値最適化や将来収益改善の証拠にしない。

## 較正の再構築・検証結果

保持している82か月（2019-11-29〜2026-08-31）を旧cache再利用なしで再構築した。panelは309,073行、forwardは1,545,775行、resolvedは1,078,765行。全cohortでforward保存済み、`integrity_check=ok`、foreign key違反0、Nomination unionと保存rankの不一致0を確認した。calibration method hashは全行`8f4cb3531b5e9694`、production hashは`fa6d6ff5dd5c183f`である。

既存評価ownerで全cohortを評価した。E[r] contextに使える3yは40か月、5yは18か月、共通は17か月（2020-01〜2021-07、2020-06と2021-04を除く）。必須34組を満たし、`evidence_complete=true`、blockerなしで[新context](../../published/er-level-calibration-latest.yaml)を生成した。有効期限は2026-11-06。旧artifactへhashを付け替える操作はしていない。

Candidate Discovery fidelityも全horizonで評価した。82か月中78か月は保存済みJPX規制入力がなく、残る4か月も長期forwardが未成熟である。union / Reinvestment fidelity eligibleは3mの1か月のみで、6m・1y・3y・5yは0。これは既存coverageを正直に反映した結果であり、長期Candidate Discoveryの実証的な改善を主張できる状態ではない。TTM期間のcorrectnessはsourceからの結合テストと固定断面比較で検証し、fidelityの不足を理由に閾値・nomination_depth・入力を調整していない。[検証集計](./calibration-validation.json)にscopeと判定を保存した。

再生成に使用した正規commandは`screening calibration-build --start 2019-11-01 --end 2026-08-31 --force`、`screening calibration-evaluate`である。context評価には`--run-purpose empirical_change_evidence --required-metric er_level_calibration`と共通17か月の`--required-asof`、`--context-out reports/published/er-level-calibration-latest.yaml`を指定した。Candidate Discoveryは`--decision-subject candidate_discovery_nomination_union`のdiagnosticを別に実行した。

## 実装検証・レビュー

- Python: 3,073 passed、8 skipped、coverage 85.13%。skipは専用R2 acceptance認証が未設定の8件。
- Frontend: 231 tests、lint・build成功。Worker: 88 tests、types・typecheck・deploy dry-run成功。
- Ruff format / lint、mypy、import boundaries、全drift gate、Bandit、runtime/dev/build依存のpip-audit、Frontend/Workerのnpm audit成功。既存warningは修正対象に含めていない。
- `km-review`に沿って4WSの要求・実装・周辺consumer・回帰テストを確認。MCP cursorの数値型への追従漏れを実装工程で解消し、公開MCP入口のページ送りも再検証した。最終判定PASS、未解消blockerなし。

main反映後は、較正snapshotを通常作業場所へ切り替え、Frontend deployとcloud-materializeを確認する。既存Run・Review Set・Triage等のhistorical artifactはそのまま保持し、新しい計算は次回の通常screening生成に適用する。
