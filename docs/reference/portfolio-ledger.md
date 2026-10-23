# Portfolio Ledger

repositoryの確認済み取引・資本だけをapplication DBのeventとして保存し、cash・予約・保有をreplayする。broker残高の自動取得・推定・完全照合ではない。価格は評価時にmarketから読み、台帳へ第二の価格正本を置かない。操作は[Ledger Record](../../.agents/skills/ledger-record/SKILL.md)。

## 事実と再送

eventはappend-only。遅延報告も新規rowとしてreplay順へ追加し、既存IDや同時刻順を変えない。日付報告の内部occurred_atは順序キーで、実発注・約定時刻の推定ではない。任意の時刻・期限を記録の前提にしない。「今週中」やrouteは原文で残せるが正確な期限・実約定市場へ変換しない。

同じ日付・内容のIDなし約定報告は再送として扱う。同日別約定を区別するにはbrokerのexecution IDを使い、注文内だけで一意なら注文IDとの組を渡す。注文ID単独や推定時刻では区別しない。同ID同内容は予約消滅後もno-change、異内容は拒否する。

買付は具体buyを公開したProposalまたは直接取引のmanual-fact参照へ結ぶ。後続は同じ参照・reservation・orderを使い、別のactive注文へ推定対応付けしない。提案なしの事実記録のために評価・提案を作らず、旧bindingを新規評価として読まない。実売却の判断参照は任意である。

<a id="human-result-semantics"></a>
## 予約・約定・cash

買予約は残数量×guardを未予約cashから拘束し、売予約は保有数量を拘束する。売付の見込代金をcashへ加えず、約定まではlotを減らさない。partial fill後は未約定分だけを残し、取消・失効は人間確認のreleaseで解放する。時刻だけから自動releaseしない。

保存注文へ対応する約定と、注文未登録の直接約定はどちらも記録できる。発注時刻や架空の注文を補完しない。他注文に拘束されていない株数の独立売却も扱い、その結果で既存予約を勝手に減らさない。拘束済み株数の報告は対応注文または取消事実を確認する。

売買代金とincomeはgross、費用と確認税は別event。売却代金は約定時にcashへ計上し、available cashは台帳上の未予約現金であって受渡済み資金・broker発注可能額ではない。FIFOの実現gross損益は台帳開始から通算し、全売却・買直しでリセットせず、既にcashへ入った売却損益を再加算しない。

予定入金や未報告取引はcashへ加えない。明示された費用0は0 eventとして保存できるが、eventなしは費用・税ゼロの確認ではない。推定exit taxは表示計算でcashやconfirmed taxと混ぜない。

## 時価と権利basis

raw/unadjusted closeと現在保有episodeの数量basisを合わせる。取得済み日次終値へ架空の15:30観測時刻を付けず、立会終了による利用可否と観測日を分ける。価格調整basisと株数basisは別で、rights issueから既存数量や権利行使を推定しない。

quote不足・古さ・権利未解決は対象holdingの時価を未評価にする。cash・予約・数量・原価を保持し、全保有が評価できなければNAV・集中比率を作らない。cash-only代用や旧台帳価格で穴埋めしない。再評価だけでledger_as_ofを進めない。

## Draftとapply

draftは正本を変更せず、確認した入力状態と差分を持つ。人間確認後にapplyし、staleや矛盾では書かない。取引保存に現在の購入適格性や無関係なmarket更新を要求せず、cash・数量・参照・報告の整合を検証する。

台帳読取は未作成/未登録と破損を区別し、table欠損・不正payload・metadataなし取引を空保有へ変換しない。現在の判断・注文条件から過去取引を改変しない。復旧・本番反映は[OPERATIONS](../../batch/OPERATIONS.md#application-db-を復元する)に従う。

<a id="historical-outcome"></a>
## Historical Outcome

資産全体を各JPX営業日closeまでreplayし、cash・予約・open holding時価からNAVを求める。入金/出金はexternal flow、売買・予約・income・cost・taxは内部event。実費・税は記録済みだけを反映し、resolvedも未報告分の不存在を保証しない。

期間は開始日closeから終了日close。開始NAVへ反映済みeventを期間cashflowへ重ねず、非営業日eventは次の営業日BODへ繰り越す。開始前の分割と全売却日のactionも含む保有episodeの単位を確認するが、購入当日のactionを過去保有の変更としない。calendar・価格・basisは同じSQLite読取snapshotを使う。

価格欠損、未解決action、不正NAV・分母は補完せずunresolvedにする。確認済み損失・費用で正の資本が0になった場合は欠測でなく-100%の実績。保存Outcomeはimmutableで、過去IDへ新形式や現在値を上書きしない。

primary benchmarkは同期間のJPX TOPIX gross total return。ETF price-onlyの市場方向評価を転用しない。指数とportfolioの税・費用basis差を示し、売却済み銘柄や勝率だけで成果を測らない。
