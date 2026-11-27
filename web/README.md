# 保有・運用のWeb

Webは[エンジン](../docs/architecture.md#変更する場所)が保存した内容を読む。提案や注文を作ったり、台帳を更新したりしない。

<a id="表示の意味"></a>

| 画面 | 何を、いつの値として読むか |
| --- | --- |
| 資産状況 `/` | 人間が報告した台帳を再生し、市場の未調整終値と株数の単位が合う保有だけを時価評価する。台帳の更新日と株価の日付は別。運用成績は新たに計算せず、保存済みの結果を表示する。[台帳・時価・成績の意味](../.agents/skills/ledger-record/SKILL.md) |
| 売買提案 → 日本株 `/proposal` | [保存提案](../docs/reference/trade-proposal.md)の履歴を発行時刻の新しい順に、JSTの日付・午前/午後でまとめる。銘柄を選ぶと、その提案IDと銘柄に対応する当時の評価・独立検証・条件を読む。現在価格や現在の注文可否を再判定しない。 |
| 売買提案 → 米国株 `/us-proposal` | [保存済みのUSD判断](../docs/reference/us-research-mvp.md)の直近20枠。提案IDで保存詳細を読む。見送り・判断保留、根拠、情報締切と確認・査読時刻、見直し条件を読む。同期間のUSD現金比較は保存済みなら表示し、根拠不足なら未確定と示す。 |

上部ナビの「売買提案」は一つ。提案内で市場を切り替える。日本株の詳細と米国株の判断は別の保存項目を使い、通貨や判断の意味を無理にそろえない。

## 提案レポートの読み方

同じ銘柄でも別の提案は別のレポートである。日本株では銘柄の要点を一覧に出し、詳細には理由の全文、七つの観点、根拠・反対論・未解決の問いを残す。留保を切り落とした抜粋や、複数銘柄に同じ見出しを使った要約はしない。提案全体の比較、訂正、人間の報告は銘柄別判断と分けて読める。[要点の書き方](../docs/reference/trade-proposal.md#report-summary)

チャートの「当時の作者判断」と「固定資料の観測」は別物である。作者が不足・対象外とした観点には後から判断を足さない。作者が記録しなかった観点に限り、当時の選定に固定した資料から読み取れる観測を別欄に示す。提供元の年度EPSも当時の出典・受領時刻を伴う参考値で、投資仮説への賛成票ではない。[観測と根拠の投影](../engine/src/baibai_engine/investment_judgment/trade_proposal/query.py)・[チャート](frontend/src/components/ModelPerspectives.tsx)

評価の見返りは、参照した評価の価格・期間・Base/Downsideから計算する。提案指値での条件付き比較や保有判断時の終値とは区別する。ある判断や表示項目が未記録でも、ゼロ・中立・安全とはしない。確定評価の参照が破損した提案は読取不能として件数に含め、後発評価や別の銘柄で埋めない。評価未完成の銘柄名・業種を補う場合も、その選定で固定された当時の銘柄情報だけを使う。

## 実装と配信

- [表示生成](backend/src/baibai_web/materialize.py)は[読取元](backend/src/baibai_web/sources/)から[表示モデル](backend/src/baibai_web/readmodel/)を作る。生成、公開、認証済みレスポンスの読戻しは別の段階である。
- [履歴画面](frontend/src/pages/ProposalHistoryPage.tsx)は発行履歴を使う。`/api/proposal/latest` は履歴の先頭ではない。訂正対象を除き、提案基準日を優先して選ぶ正式な最新一件で、[配信照合](../tools/diagnostics/check_served_proposal.py)も区別して検証する。
- [両市場の最新・履歴](backend/src/baibai_web/materialize.py)は、一つのAppスナップショットの保存内容だけを読む。過去のLake・Marketは不要。「表示データ生成」はこれらの表示を作った時刻であり、提案発行時刻や過去時点の絞り込みではない。日本株の配信照合では、訂正された提案も含め、表示の生成が履歴内の発行より前でないことを確認する。
- [生成ツール](backend/src/baibai_web/contracts_export.py)がPython表示モデルからschema・TypeScriptを作る。生成物は手編集しない。[Worker](edge/src/index.ts)は[配信経路](contracts/routes.json)の全viewに共通認証を適用する。
- `views/proposal-history.json` は現在の提案履歴に使う配信物。`/api/health` はR2を読まず、[表示更新](../jobs/OPERATIONS.md#web-serving)は現行viewだけを上書きして旧キーを削除しない。[TradingView用TXT](frontend/src/components/TradingViewWatchlistExportButton.tsx)は画面の保有銘柄からブラウザで作るため、専用のR2ファイルはない。

- 米国株は認証が必要な `/api/us-proposal/latest` の同じ配信物を使う。`history` は発行時刻・発行ID順の直近20枠（読取不能な枠も数える）。履歴が読める場合、正常な最新 `proposal` を先頭に置き、重複を含まない `history.proposals` を続ける。最新が不正でも別の判断を最新にしない。IDが正しい読取不能の枠だけIDを示し、古い枠を繰り上げない。範囲外の正確な保存判断は[保存元の読取](../docs/reference/us-research-mvp.md#隔離appに米国株の見送り判断保留を保存する)で確認できる。
- 未配信・読取不能と保存済み判断0件は別。Web配備と配信物の更新は独立し、データ形式の切替中は一時的に表示できないことがある。切替後は認証済みレスポンスで履歴と、保存元と同じ最新の発行ID・査読SHAを確認する。画面は履歴のない旧配信物から履歴を作らない。

環境準備とViteで確認できる範囲は[開発手順](../docs/development.md#webの確認)、本番での生成・公開・読戻し・Worker配備は[運用手順](../jobs/OPERATIONS.md#web-serving)へ。
