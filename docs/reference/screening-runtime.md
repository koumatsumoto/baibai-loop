# Screening

保存済みの五モデルから調査候補を抽出する。モデル生成は候補選定より先に完了し、候補外の企業もInvestment Judgmentから読める。候補政策は企業の経済的reject・価値評価・購入判断を代行しない。

<a id="rule-purpose"></a>
## 候補抽出の意味

初期政策は、報告利益と価格の座標Yを使うviewと、Yに中期価格変化Mを組み合わせるviewのunionを調査入口にする。抽出枠は有限の調査容量であり、投資枠や採用件数ではない。順位算式・tie-breakと政策型は[views.py](../../engine/src/baibai_engine/machine_analysis/screening/views.py)、観測窓はモデル側の実装を正本とする。重複候補を二票として扱わない。

候補理由はview別rankとして保持し、union全体の投資順位を作らない。Yは期待収益率ではなく、Mは配当込みreturnではない。同じ価格を含む二つの座標を独立した証拠票にしない。未知を0へ置換せず、Yのみ既知ならY viewだけへ参加できる。負利益を一律排除せず、極端な利益利回りを持続する現金創出の証拠にしない。

<a id="candidate-discovery"></a>
## 調査への引渡し

機械E[r]/FV、四Approachの旧membership、保存市場判断、portfolioは新しい候補membership・順位を決めない。Investment Judgmentは五モデル・一次資料・必要な少数の候補外対照を比較し、価値獲得経路と反証を自ら評価する。旧run・旧Selectionは保存済みidentityと内容のまま読めるが、新しい標準経路へ混ぜない。旧runを新規生成するscreening run入口は撤去し、旧較正は当時のモデル契約を明示して別に扱う。

## 入力不足と品質

普通株masterから対象集合を固定する。価格日、財務対象期間・公表日、計算基準日、外部評価の取得時点を区別し、欠測・既知0・負値を保つ。取得coverageは成功した要求範囲で、全銘柄の入力充足ではない。Yの価格は共通の直近営業日と一致する場合だけ候補へ渡し、古い価格・利益・時価総額は観測日付きで保持する。Mは最後253営業日の起点からas_ofまで全暦日が連続して確認できなければ欠測とし、calendarの穴で端点をずらさない。Yの直近日確認はMの長期履歴充足とは独立させる。各モデルの不足はその材料の不足として示し、候補数を満たすために別値で埋めない。

共通統合は非営業日も経済・外部評価を更新できる。as_ofが新しくても株価観測日を当日へ書き換えず、保存原本からの再計算を完全な当時point-in-time再現とは扱わない。EDINETの原本取得・offline抽出・訂正状態はsource統合で扱い、L3が必要な一次資料とCF/BSを追加確認する。

## 保存と読取

ModelGenerationは入力identity・計算revision・モデルを一組として保存する。ScreeningSelectionは別identityで、元generation・対象母集団・政策・結果を保存する。既存identityへの別内容の上書きは拒否し、再送で初回の公開時刻を変更しない。候補0件も正常な公開結果であり、過去の非空候補へ戻さない。

既定読取はresearch日以下の基準日を優先し、同日は公開時刻で選ぶ。generation評価時刻と候補公開時刻の両方がknown_at以下でなければならない。explicitの未知・未来identityをlatestへ差し替えない。Selectionは必要なモデル断面と政策結果をapplication DBへ複写し、再開は保存入力だけを読む。日次の再生成cacheは最近の公開政策に必要な世代と今回の明示生成を優先して有限に保持する。保持数・順序は[所有実装](../../engine/src/baibai_engine/machine_analysis/generation/store.py)を見る。削除は同じreleaseの公開成功後に限り、保存済み評価・提案・台帳・原本・較正とは別である。

取得・retry・受領反映・発行順は[OPERATIONS](../../jobs/OPERATIONS.md)が所有する。意味を変えない配置変更だけで計算revisionを更新しない。
