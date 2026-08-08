# 日次デルタの着手遅延 baseline

価値tier: T1 — 候補・保有の変化が現れてから人間の評価が記録されるまでを測り、日次デルタ観測層が注意配分を早めたかを四半期ごとに判定できるようにする。

## 事前登録

この節はbaseline値の算出前に固定する。measurement as-ofは2026-08-02（JST）とし、入力storeにこの日より後のrecordがあっても使わない。日数はJSTの暦日差で数える。

### 候補側

- machine poolはrun storeの`screening_selection.payload.longlist`だけとする。全candidate配列やproduction cap後の`recommendations`をlonglistの代用にしない。
- 同一as-ofに複数のmachine selectionがある場合、shortlistが1つのselectionを明示的に束縛していればそれを採る。束縛が無ければ`created_at`が最新のnon-empty longlistを採る。異なるselectionを複数shortlistが束縛する日はambiguousとしてpoolを推定しない。
- tickerの`first_observed_pool_date`は、保持中のlonglist snapshotで初めて現れたas-ofとする。coverage開始日より前、または途中にlonglist欠損日がある場合は`left_censored`とし、真のfirst-seenを主張しない。
- 評価日は、そのtickerを`selected`または`rejected`として持つ最初のshortlistの`published_at`をJST日付へ変換した日とする。shortlistには着手時刻が無いため、これは**評価着手そのものではなく、評価がcanonicalに記録された時点のproxy**である。`as_of`を着手時刻として扱わない。
- 遅延は`evaluation_recorded_date - first_observed_pool_date`。first-seenがleft-censoredなら値は下限としてだけ出し、exactな遅延分布へ入れない。
- exact遅延の分布は件数・最小・中央値・最大と、`0日 / 1日 / 2〜3日 / 4〜7日 / 8日以上`の固定bucketで出す。標本が無い統計量はnullとする。
- 捕捉率の分母は観測できたlonglistのunique ticker、分子はmeasurement as-ofまでに同日以後のshortlistで評価が記録されたticker。未評価はright-censoredとして件数とtickerを残す。
- run storeの保持期間とlonglist snapshotの有無を日別に出す。R2の`history/candidate-views/`は31日保持だがlonglist membershipを持たないため、このbaselineのfirst-seen sourceには使わない。

### 保有側

- 母集団はmeasurement as-of以前のcanonical ledger headでopenなholding。ledger headがmeasurement as-ofより古ければ、その翌日以降をposition coverage gapとして明示し、現在保有を推定しない。各tickerについて、measurement as-ofまでにpublishされた最新thesisの`estimates.current_fair_value_yen`を使う。
- FV factの発火日は、thesisの`published_at`をJST日付へ変換した日以後で、market storeのunadjusted closeが初めてFV以上になった営業日とする。日次デルタと同じ価格basisを使い、thesisが人間に利用可能になる前の到達を遡及認定しない。
- thesis publish日から観測日までに株式分割・併合を示すnon-1 `adjustment_factor`があれば、FVの株数basisを機械補正せず`corporate_action_unresolved`とする。
- holding review実施日は、発火日以後で最初のcanonical holding reviewの`as_of`。発火前のreviewを捕捉として数えない。遅延はその暦日差とし、reviewが無ければright-censored日数を出す。
- review済み遅延も候補側と同じ固定summary・bucketを使う。FV発火済みholdingを捕捉率の分母、後続review済みを分子とする。
- FV欠損、価格欠損、未到達、corporate action未解決を空の結果へ畳まず、coverage status別に計数する。

### 判断境界

初回は少数標本と短い保持窓を件数で開示し、日次デルタ導入の効果を主張しない。自動cycle起動、自動task起票、shortlist判断、holding action、FVは変更しない。四半期更新では同じCLI・定義を再実行し、exact observationが蓄積してから導入前後を記述比較する。

## 初回結果

実行コマンド:

```bash
.venv/bin/python -m tools.experiments.measure_daily_delta_effect \
  --as-of 2026-08-02 \
  --out /tmp/daily-delta-baseline.yaml
```

出力artifact SHA-256は`2c999608fd08ebf49c6b46d35d6d8776a91bd615238788e67be85336fa45e0ed`。入力storeはapplication DB `d99b258ed4cb4c763d9bf154d792aef092618e2627d2e6afce4b1c665c5caa62`、run store `9792f62d8e0508ed16bf917aa97fdbd0a2240244d60f38800913e0075825839b`、market store `8d039f2219a981bb54dbbbdbbe098a7ad5efdcdbac077a80502cdae071c4ae70`だった。

### 候補側

| coverage / metric | 結果 |
| --- | ---: |
| retained run日 | 3日（2026-07-27〜07-29） |
| explicit longlist snapshot | 1/3日 |
| longlist unique ticker | 20件 |
| 後続shortlistで評価記録済み | 20件（捕捉率100.0%） |
| exact first-seen | 0件 |
| left-censored first-seen | 20件 |
| exact遅延分布の標本 | 0件 |

07-27と07-28のretained machine selectionは`longlist`を持たず、07-29のshortlist束縛selectionだけが20件のlonglistを持っていた。20件はすべて07-30に同じshortlistで評価が記録され、観測できた下限は1日だった。ただし前2日のlonglistが欠けるため20件すべての真のfirst-seenは左打切りで、1日をexact遅延として採点しない。捕捉率100%も1 snapshot・1 shortlistの記述であり、日次デルタの効果を示さない。

R2の31日`candidate-views`は全candidate rowを保存するが、explicit longlist membershipを保存しない。FV欄の有無からmembershipを推定するとFV欠損longlistを落とし得るため、baseline入力から除外した。四半期更新でも利用可能なcanonical longlist snapshotだけを読み、保持窓が改善しない限りexact件数ゼロをそのまま報告する。

### 保有側

| coverage / metric | 結果 |
| --- | ---: |
| ledger head時点のopen holding | 10件 |
| 最新thesis / FVあり | 2件 |
| FV未到達 | 1件（3836） |
| FV到達 | 1件（4432） |
| 到達後holding review | 0件（捕捉率0.0%） |
| exact review遅延分布の標本 | 0件 |

canonical ledger headは2026-07-15 09:00 JSTで、07-16〜08-02のposition stateはcoverage gapである。head時点のopen holding 10件のうち8件はthesisが無く、価格とFVを比較できたのは2件だった。3836はmarket store終端の07-31までFV未到達。4432は07-29のunadjusted close 3,230円でFV 3,093円へ初めて到達したが、その後のholding reviewは無く、08-02時点で4暦日right-censoredだった。07-14のholding reviewは発火前なので捕捉に数えない。

### 初回判定

候補遅延はexact標本0件、保有遅延はreview済み標本0件であり、日次デルタが着手を早めたかは判定不能。観測層、自動cycle、自動task、shortlist判断、holding action、FVは変更しない。次回は同じCLIと固定bucketで再計測し、coverageと打切りを含めて比較する。
