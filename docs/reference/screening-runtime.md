# Screening

保存モデルから調査する理由を見つけ、有限のResearchへ渡す。候補・表示順・経済経路のeligibleは、割安性・健全性・買付採否ではない。判断は[doctrine](../doctrine.md)、開始/再開は[Investment Judgment skill](../../.agents/skills/investment-judgment/SKILL.md)へ。

旧版も当時の内容・理由・順序・identityで読む。未記録の区画を現在のモデル・既定値で補わず、現政策で再選抜しない。

## 固定して読む

初回`selection-context`のSelection ID、`research_as_of`、`input_known_at`を保存する。続きも同じ依頼scopeで、それぞれ`--screening-selection-id`、`--research-asof`、timezone付き`--known-at`へ渡す。`--known-at`省略は実行時刻なので、固定した続きには使わない。

| 読みたいもの | 同じ`selection-context`へ追加する引数 |
| --- | --- |
| レーンの続き | `--screening-lane <LANE> --screening-page <next>`。ページ番号は0始まりで、返されたnextを使う。 |
| 候補外を含む個社の完全モデル | `--compare-ticker <TICKER>`。候補へ追加したりResearchを開始したりしない。 |
| T合致でも現在の調査仮説がない行 | `--screening-excluded`。保存T合致かつ`no_current_hypothesis`を読む。 |
| T合致を前提にせず比較不足を探す | `--screening-excluded financial_unresolved`。保存G未確定かつ`no_current_hypothesis`を読む。 |

`candidate_tickers`は表示中のページ、`summary`は全stock planの分母。除外診断は通常レーンへ復帰させず、元の非充足・不足理由を保つ。条件ごとの件数は同じ会社が重なるため加算できず、失った投資機会や棄却の正しさの測定ではない。0件も入力の完全性を意味しない。

Research・市場判断は保存/公開時刻をcutoffと照合する。一方、保有は現在台帳を事実時刻でreplayするため、過去の注文/約定の後追記で同じcutoffの対象が変わり得る。この読取を、[保存Operationの固定入力からの再開](../../.agents/skills/investment-judgment/SKILL.md#完了と再開)や当時既知の情報集合と同一視しない。

## 発見根拠と三つのレーン

同じstock plan全社の[技術・市場の発見根拠（T）](../../engine/src/baibai_engine/machine_analysis/screening/policies/technical_discovery.py)、[財務・成長仮説（G）](../../engine/src/baibai_engine/machine_analysis/screening/policies/financial_growth.py)、[財務経路（F）](#rule-purpose)を併読する。T非合致や価格不足でも財務仮説を落とさず、Gを全企業の普遍的な合否にしない。旧財務上位候補や経済順位で再足切りしない。

| レーン | 調べる理由 |
| --- | --- |
| `growth_research` | Gの利益成長/営業回復仮説。通常表示だが、正式Research採択ではない。 |
| `input_review` | G未確定にT/Fの根拠がある予備調査、または独立した事業仮説に基づく価格/株数入力の確認。 |
| `alternative_financial_research` | G非合致でも別のF仮説がある。Gの既知悪化・不足を消さない。 |

所属条件と保存版の読取は[policy owner](../../engine/src/baibai_engine/machine_analysis/screening/policies/technical_financial.py)、価格入力確認は[座標のowner](../../engine/src/baibai_engine/machine_analysis/screening/policies/economic_path_inputs.py)が持つ。株数不足・資本異動後の自己株未確認・明示的な古い価格は、必要な事業根拠と他operandがある場合だけ確認理由になる。未知一般・モデル未生成を候補へ戻す仕組みではなく、元の赤字・非適格・価格座標nullも保つ。古い価格を現在価格へ繰り上げず、取引停止・流動性・現在の注文可否は別に調べる。

表示は企業重複を除く決定的な経路巡回。現行はレーン内で会社共通の固定hashを使い、コード番号を調査価値の代理にしない。経路は独立票でなく、小群・経路重複による露出差と先頭ページ反復の偏りは残る。ページ容量は候補足切り・投資順位・調査枠ではなく、枠消化や合致数の加点もしない。旧版の順を現在hashで並べ直さない。

三レーンの分離は、T必須の見落としと全経路ORの無差別な拡大を比べる設計仮説で、閾値・表示順の最適性や利益優位は未検証。L3は支持/反証、必要な候補外対照、答えが得られ判断を変える問いから対象を選ぶ。未確定なら先に答える問い、既知非充足の例外なら根拠を示し、資金耐久・価値・買値・待機・最強反証をAssessment/Review/Proposalへつなぐ。

<a id="candidate-discovery"></a>
## 初回カードから調査へ

カードは同世代の保存モデルを併読する入口で、新しいscore・採否gate・注文制限ではない。未記録fieldは省略し、明示null/false/0と区別する。詳細と省略分は同じSelectionの`--compare-ticker`へ進む。原数値・符号・期間・basis・出典と反対材料を一緒に読み、モデル未生成や旧保存へ区画を補作しない。

| 比較材料 | 読み違えないための境界 |
| --- | --- |
| 成長・回復と会社予想 | 単Qの営業回復、年次/TTM、会社通期予想は別。減収との併存や前年OPの符号を読み、利益差だけで有機成長・一時要因・将来の資金余力を認定しない。会社予想の赤字/黒字も自動除外/安全認定ではない。 |
| 利益額・EPS・配当 | 会社予想EPSのas-of株式basis、報告basic EPSの累計期・平均株数、現在/期末株数、希薄化後EPSを混ぜない。欠測EPSを利益額÷現在株数で復元せず、符号差の原因を推定しない。配当予想は利益予想と別の期・出典・株式basisを持ち、未記録/nullを無配、計画を支払実績にしない。配当は任意。 |
| 簿価・報告残高 | 選択簿価の算式/元開示と利益の測定期を別に読む。古いBPSと現在株数、資産と自己資本比率を同時点の普通株資本とせず、出典保持を平均資本ROEの確認済みにしない。現金は拘束・運転資金・金融業の必要資金を控除した余剰ではなく、debt欠測は無借金でない。 |
| 営業CF・投資支出 | 利益成長と資金化は別。原測定期・TTM・EDINETの別期フローを混ぜず、部分内訳を合計へ合わせたり、不足を0/単純年換算で埋めたりしない。規模不一致で不採用の原観測も表示されるため、掲載だけで比較成立としない。EDINETの不足を別出典の短信CFや全フローへ拡大せず、銀行CFや報告FCFを持続的な普通株主cashにしない。 |
| 技術・市場の価格と活動 | 値・窓・不足・比較指数/条件日数を伴わせる。相対超過と絶対騰落、倍率と円規模は別。市場下落日の平均を全区間の上昇/勝率、別々の平均を同日の価格×活動の交差、状態を新規クロスや機関買いとしない。活動の基準窓を当日を含むProposal ADVへ取り替えない。 |
| 会計変更・資本異動 | 明示注意を原公表日/対象期と読み、空一覧やfalseだけで比較可能性を確認済みにしない。公表・効力・受領の時計を分け、完了観測を現在株数の反映完了や将来希薄化としない。限定件数と省略数を保ち、同じ原資料の再受領を独立証拠へ数えない。 |
| 共有材料との関連 | Generationに明示された関連と保存理由を読む。一事業の関連を連結全体の曝露量・記事の調査母集団への所属とせず、業界の期/利益basisと会社への伝達を別に調べる。未記録理由を現在の業種分類から補わず、関連の有無を候補条件・注意順・支持票にしない。 |

観測の詳細な意味は[開示と会計比較](model-observations.md#financial-periods)、[資本・価格・配当](model-observations.md#capital-basis)、[市場の比較](model-observations.md#market-context-reads)に集約する。決算期差・古い年度補助・科目別出典を保ち、出典日の最大値を全体の公表/利用可能時刻にしない。外部評価・共有環境も比較材料で、保存市場判断やportfolioは機械順位を変えない。

<a id="rule-purpose"></a>
## 保存済み経済経路政策の意味

収益力・事業拡大・営業回復・資本活用の価格/事業座標は[入力owner](../../engine/src/baibai_engine/machine_analysis/screening/policies/economic_path_inputs.py)、保存順位と容量の型は[保存型owner](../../engine/src/baibai_engine/machine_analysis/screening/policies/economic_paths.py)へ。通常の発見レーンとは別に、旧保存結果の順位も当時のまま読む。

市場全体が割高でも相対上位は生じる。業種の異質性・小さい参照群・経路数・一過性利益の偏りがあり、順位を期待収益/確率/健全性にしない。成長/回復は利益・CFの黒字を必須にせず、売上/営業改善÷株式時価総額も負債込み事業価値の割安性を保証しない。完全FYはTTM対応期未観測の補助で、既知の悪化・basis不一致を迂回しない。資本経路の報告利益/含意簿価はproxyで、普通株ROE・換金額や未実現資産の全機会を示さない。

## 入力不足と品質

[原典・時点・不足の規則](model-observations.md#source-owners)に従い、未観測・比較不能・業種不適用・非正の分母・既知非充足を分ける。全てを再取得で埋まる欠測とせず、金融業のN/Aを赤字へ、親会社/経常利益をOPへ置き換えない。使わない回復経路のN/Aで別の利益成長仮説を消さない。

技術・財務統合政策の価格入力集計は、その世代のTA準備状態であり、財務モデルの価格basis・現在の鮮度/取引可否や全財務入力の充足とは別。候補0件、母集団0件、価格不足を分け、取得coverageや有効な価格だけで「投資機会なし」としない。

## 保存と読取

ModelGenerationとScreeningSelectionは別identity。通常生成で政策を適用し、読取から再適用/再生成しない。固定Generationへの現政策再適用と全社価格/株数不足一覧は提供せず、日次再実行を代替にしない。0候補も正常に公開し、古い非空結果へ戻さない。[保存](../../engine/src/baibai_engine/machine_analysis/screening/selection_store.py)・[時点選択](../../engine/src/baibai_engine/machine_analysis/screening/selection_query.py)、[正本/寿命](../architecture.md#store-authority)、[日次の保持・再開](../../jobs/OPERATIONS.md#日次実行と結果確認)を使う。

新政策の本番受入では既存runの終端・writer/lease・実行commitを確認し、その実行で全既存Generation/Selectionを明示保持する。これは通常cache整理後の長期保管保証ではない。切戻しも新結果と固定Researchのreader・現lease ownerを維持し、旧bundle盲復元・新結果削除・同じidentityへの旧政策上書きで行わない。

## 読取専用の研究診断

次のcallerは私有copyから新しい私有JSONへ出す診断で、通常政策の再適用・Research開始・保存・売買はしない。非zero終了の出力を採用せず、私有JSONを公開log/artifactへ出さない。

### Case固定後の探索証拠

新規Researchは当時の発行物から、深掘りする対象とdraftの任意の `comparison_tickers` の行・同世代モデルだけを固定する。明示対照は別の使い道・見送りを後日比較するための証拠で、深掘り・提案・売買対象にはしない。母集団・所属・欠測の識別は残すが、調べなかった全社の明細は再開時に現在headから補わない。L2本文の指紋と、Proposalが参照するCaseの指紋は別である。旧V2–4の保存・署名・訂正を新書式に移さない。

<a id="decision-cohort-diagnostic"></a>
### 現行判断の比較前診断

[完了Operationの比較caller](../../tools/experiments/inspect_decision_cohort.py)は保存入力・評価pair・Proposalを照合する。

```bash
uv run python -m tools.experiments.inspect_decision_cohort \
  --db <PRIVATE_APPLICATION_COPY> --operation-id <COMPLETED_OPERATION_ID> \
  --out <NEW_PRIVATE_JSON>
```

候補と調査対象のunionを残し、0対象でも候補を消さない。全レーンの保存閲覧集合と順序を使い、予備や候補外の明示調査を経済的適格へ格上げしない。運用用SelectionとProposalの原公開Selectionは役割/hashが異なり、正当な移行は既存receiptで照合する。未選定・未完・経済的見送りを分け、全開始Operationを分母とする比較や第二の判断正本、[旧較正](estimate-calibration.md)の再生成には使わない。

この診断も完全PIT・事前登録・利益/指値の因果効果を証明しない。正式比較の対象/窓/metricは結果を見る前に決め、保存完了時刻を約定可能時刻とせず、選定/提案の公開以後と取引可能時刻を別に確かめる。後日の訂正・本人報告・取引・Retrospectiveを当時本文に混ぜない。有限の政策比較と残る受入は[#1680](https://github.com/koumatsumoto/baibai-loop/issues/1680)・[#2184](https://github.com/koumatsumoto/baibai-loop/issues/2184)で追跡し、機能受入を投資効果へ換算しない。
