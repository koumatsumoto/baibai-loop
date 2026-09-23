# Reinvestment自己資本basis修正の固定断面影響（#1344）

## 比較条件

- as-ofは2026-09-18。既存のlocal market store（SHA-256: `560d270241090af14a5e6bdb426efdee9c9ae73584090a62bedb9a272e825c1f`）を読み、現行mainを基点とする実装で一時run storeへScreening Run 3,703件を生成した。元のrun storeや判断artifactは更新していない。
- 同一のSecurity Analysis入力に対し、旧式の `total_assets × equity_ratio + debt - cash` と新式の `same_state_equity_yen + debt - cash` だけを切り替えてReview Setを再計算した。TTM営業利益、sales、FCF、他3 Approach、rulesの閾値・depthは同じ。
- 旧式は比較用の一時処理に限る。採用するmethod identityはReinvestment v4、dated rulesとCandidate Discovery method hashで表す。
- これはcorrectnessの影響測定であり、将来収益や投資判断の改善を主張するものではない。

## 結果

| 項目 | 旧式 | 同一行の自己資本 |
| --- | ---: | ---: |
| 独立繰越のTA/EqARがともに正 | 3,682 | — |
| 同一行のTA×EqARが正 | — | 3,682 |
| 両式の自己資本額が異なる銘柄 | 2 | 2 |
| Reinvestment input population | 802 | 802 |
| Reinvestment eligible | 52 | 52 |
| market capital-return floor | 15.153405% | 15.153405% |
| market operating-margin floor | 8.180483% | 8.180483% |
| Reinvestment top20 | 20 | 同一20銘柄・同一順位 |
| Nomination union | 76 | 同一76銘柄 |

sector別capital-return floorも表示精度内で同一だった。他3 Approachのtop20は順位まで同一。2銘柄の個別値は次のとおり。

| ticker | 旧式の自己資本 | 同一行の自己資本 | Reinvestmentへの影響 |
| --- | ---: | ---: | --- |
| 7363 | 621,328,944円 | 716,400,000円 | capital returnが41.008711%から34.239640%へ変化。eligible/top20は不変 |
| 9628 | 48,444,480,000円 | 38,305,872,000円 | 他の必須入力が成立せず、capital returnは生成されない |

今回の固定断面で候補集合が変わらなくても、7363のcapital returnは実際に変わる。今回の回帰テストは、別stateのTAとEqARを掛けず、最新のcomplete actual rowだけを使う契約を直接検証する。

過去の2026-09-18公開済みReview Setとの単純比較では、#1341によるTTM営業利益修正も混入する。この記録はその混入を避け、同一run入力で資本basisだけの影響を測った。

## 較正の整合確認

保持中の2019-11〜2026-08の82 cohortを`calibration-build --force`で全再構築した。integrity checkはok、外部キー違反は0。新rules hashは`e9f780565b5f4d11`、calibration method hashは`e42d6d3c69aa566b`。同じ17か月のrequired scopeで行ったE[r]文脈評価は`eligible`かつ`evidence_complete: true`となり、公開文脈を正規手順で更新した。

Candidate Discoveryの全cohort診断も新identityで完走した。3mではJPX規制snapshotが78 cohortでunavailable、4 cohortでcompleteであり、Nomination unionのfidelityは1 cohortだけeligible。これは履歴sourceとhorizon成熟度による既存の証拠制約であり、完全な過去fidelityを主張しない。件数とstatusは[calibration-validation.json](./calibration-validation.json)に記録した。
