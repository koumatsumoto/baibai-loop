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

- 母集団はmeasurement as-of日末のcanonical ledgerでopenなholding。各tickerについて、その時点までにpublishされた最新thesisの`estimates.current_fair_value_yen`を使う。
- FV factの発火日は、thesisの`published_at`をJST日付へ変換した日以後で、market storeのunadjusted closeが初めてFV以上になった営業日とする。日次デルタと同じ価格basisを使い、thesisが人間に利用可能になる前の到達を遡及認定しない。
- thesis publish日から観測日までに株式分割・併合を示すnon-1 `adjustment_factor`があれば、FVの株数basisを機械補正せず`corporate_action_unresolved`とする。
- holding review実施日は、発火日以後で最初のcanonical holding reviewの`as_of`。発火前のreviewを捕捉として数えない。遅延はその暦日差とし、reviewが無ければright-censored日数を出す。
- review済み遅延も候補側と同じ固定summary・bucketを使う。FV発火済みholdingを捕捉率の分母、後続review済みを分子とする。
- FV欠損、価格欠損、未到達、corporate action未解決を空の結果へ畳まず、coverage status別に計数する。

### 判断境界

初回は少数標本と短い保持窓を件数で開示し、日次デルタ導入の効果を主張しない。自動cycle起動、自動task起票、shortlist判断、holding action、FVは変更しない。四半期更新では同じCLI・定義を再実行し、exact observationが蓄積してから導入前後を記述比較する。

## 初回結果

計測実装後に追記する。
