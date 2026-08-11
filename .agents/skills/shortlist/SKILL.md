---
name: shortlist
description: 買い機会の発見と絞り込み。screening run → select → OP3 narrative 付き shortlist publish → 人間の primary-research set 選択待ちまでを進める。選択後の深掘りは research skill。
---

# Shortlist

全上場銘柄から比較可能な候補群を作り、`/stocks/shortlist` レビュー面で人間へ提示する。終端は人間の primary-research set 選択（推奨 2〜4 件、上限は selection の `research_selection_target_max`）。AI は broker 操作へ進まない。

## 前提

1. AGENTS.md の session 規約に従い `operation start --kind opportunity --as-of <最新完全営業日>`（active があれば resume）。
2. `uv run baibai-engine position ledger --db stores/application/baibai.sqlite` で holding / reservation / cash を読む。
3. macro context head の鮮度を判断する: `baibai-engine macro context head` → head が古い、または[深度契約](../../../docs/reference/macro.md#depth-contract)を満たさないと判断したら、先に `macro-context` skill で書き直す。
4. 候補比較の優先順位は [doctrine](../../../docs/doctrine.md)（永久損失 → 5 年期待値と FV 乖離 → portfolio 追加価値 → 購入可能性）。cash・集中・保有はannotationであり、上位候補の hard 除外に使わない。

## 手順

1. **preflight とデータ準備**: run を作る前に cloud batch と local store と checked-out code を一度に突き合わせる。

   ```bash
   shortlist_preflight_dir=$(mktemp -d /tmp/baibai-shortlist-preflight.XXXXXX)
   batch/scripts/r2_transfer.sh pull-run-summary "$shortlist_preflight_dir/latest-run.json"
   uv run baibai-engine screening shortlist preflight \
     --asof <ASOF> --cloud-summary "$shortlist_preflight_dir/latest-run.json"
   ```

   local run store に cloud publication が無ければ `batch/scripts/r2_transfer.sh pull-runs` で cloud 正本だけを取り込み、preflight を再実行する。`decision: reuse` なら表示された `selection_id` を `screening selection show` で取得して手順 4 へ進み、同一 as-of の `run` / `select` は実行しない。`decision: resume-current-code` なら表示された `run_revision_id` で手順 3 だけを 1 回実行する。`decision: rerun-current-code` のときだけ手順 2・3 を各 1 回実行する。`previous.status: ambiguous` なら candidate の run ID を選び、同じ preflight に `--previous-run-revision-id <ID>` を足して再実行する。`decision: blocked` は理由を解消するまで run を作らない。preflight 後に HEAD または as-of が変わったら古い判定を使わず再実行する。

   ```bash
   uv run baibai-engine screening verify-cache-coverage --asof <ASOF>
   # 不足 source があるときだけ: uv run baibai-engine screening bootstrap-cache --asof <ASOF>
   uv run baibai-engine screening extract-edinet-metrics --asof <ASOF>   # coverage 完了でも省略しない
   ```

   coverage が future-dated / stale JPX なら停止（historical backfill 以外で `--allow-stale-jpx` を使わない）。
2. `uv run baibai-engine screening run --asof <ASOF>` → `run_revision_id` を保持。
3. `uv run baibai-engine screening select --asof <ASOF> --run-revision-id <ID> <PREVIOUS_ARGS> --longlist-top 20 --output-path <workdir>/selection.yaml` → `selection_id` を保持。`<PREVIOUS_ARGS>` は preflight の `previous.selection_arguments` をそのまま渡す（`previous.status: missing` だけは省略）。これにより canonical previous を別 revision へ置換せず、prune 済みなら application DB の retained shortlist entries を明示利用する。longlist 20 件は点検 view であり全件深掘りの命令ではない。`--longlist-top` は publish 時の機械行焼き込みの入力でもあるので省略しない（省略すると run が prune された後にレビュー面の機械値が消える）。
4. **供給文脈**: 手順 3 で保持した `selection_id` を渡して当日の座標を控える。

   ```bash
   uv run python -m tools.experiments.measure_supply_context \
     --selection-id <ID> [--longlist-history-dir <手順3で取得済みのDIR>]
   ```

   当日 selection 上位 5 の平均 E[r] と最新月末 panel の hurdle 超え件数が供給軸、
   `temporal_jaccard`・直近 12 月 unique top-20・carry 支配 share・sector HHI・最大
   `sector_33 × carry/reversion` cluster share・直近 shortlist の `event_wait` share が幅軸である。
   各値と歴史 percentile を報告し、**供給と幅を 1 語へ畳まず両軸を残す**。逆を向くことは
   普通に起きる。run retention で前 cycle が無い、panel の月が欠ける、shortlist が無い場合の
   `unmeasured` は 0 や異常なしへ読み替えない。前 run が prune 済みで手順 3 に R2 longlist
   history を使った場合は、同じ directory を渡して temporal Jaccard を復元する。

   供給 2 座標が同じ方向を示し、幅の各座標も同じ結論を支える場合にだけ、低供給×狭い幅を
   市場側の枯渇、十分な供給×狭い幅を集中した供給、十分な供給×広い幅を広い供給、低供給×
   広い幅を現行 value 軸外の機会として読む。供給内または幅内で方向が割れた場合は四象限を
   断定せず、割れた座標と `research / discovery` の優先判断が未解決であることを人間へ渡す。
   percentile に新しい二値閾値を置かない。ticker の新しさ自体を KPI にせず、carry 集中も
   単独で悪化と判定しない。

5. **差分確認**: 前回 shortlist（application DB）と ticker 集合を new / continued / exited で比較する。あわせて `uv run python -m baibai_engine.research_watch --db stores/application/baibai.sqlite --sqlite-path stores/market/market.sqlite --asof <ASOF>` を回し、深掘り済み ticker の現在価格と研究 FV の位置を控える（手順 7 の再研究判定に使う）。continued も narrative を自動継承せず、順位差・価格・最新開示・countercase を再確認する。前回を確認できない run は全候補を確認する。機械側の差分は `selection.diagnostics.previous_overlap` に出る。`previous_candidates_source` が `run_revision` なら母数は前 as-of の全候補、`longlist_history` なら前回 daily longlist の top-N、`canonical_shortlist` なら人間が確認して保持した shortlist entries なので、異なる source 間で重なり率を比較しない。`null` は前回が取れなかった状態で、重なり 0 件と読み替えない。
   canonical previous は preflight の `previous` と手順 3 の引数で固定済みである。`resolved` は greatest prior as-of の run revision、`resolved-shortlist` は run が prune 済みのため application DB の同日 canonical shortlist に焼き込まれた entries を使う状態である。`canonical-unavailable` は retained entries も読めないため block のままとし、同日別 revision へ代替しない。
6. **開示スキャン**: selected 候補（full review では全候補）の直近開示をタイトルレベルで確認し、as-of 財務に無い material 開示（業績修正・資本政策・TOB 等）を narrative の `why` / `counter` へ反映する。
7. **annotation 消化**（不変条件: 判断面へ annotation を足す変更は、この表へ消化規則を同時に足す）:

   | annotation | 消化規則 |
   | --- | --- |
   | FV convergence warning（`price_at_or_above_all_fv_anchors`） | selected / rejected を問わず明示消化する。黙殺しない |
   | `margin_short_to_adv` / `margin_week_end` | 需給の確認材料。単独で自動除外・rank 変更に使わない |
   | `buyback_authorization_status`（`selection.yaml` の `longlist[].buyback_authorization` / `recommendations[]`。shortlist entry へは焼き込まれない判断時の入力なので、消化した内容は narrative へ書く） | E[r] の buyback carry が forward の現金還元か、過去の資本配分の記録かを分ける材料。値は**観測そのもの**で、枠が今も在るかの推論ではない。`recent_filing` = 直近 45 日に自己株券買付状況報告書あり（取得期間が終了した月の報告書もここに入る）。`stale_filing` = 提出はあるが古い。`no_filing` = 観測窓 1 年に提出なし。`unknown` = store の観測窓が as-of から 1 年に届かない（historical run は常にこれ）。**単独で自動除外の理由にしない** — 単発で終わった還元も較正では母集団を上回る（[診断](../../../reports/studies/2026-08-06-bargain-capture-diagnosis/report.md) §6.1）。枠の中身は下の行が持つので、そちらと必ず併せて読む |
   | `buyback_remaining_share_ratio` / `buyback_trailing_3m_acquired_ratio` / `buyback_authorization_window_end`（同じく `longlist[].buyback_authorization`。`buyback_report_month_end` がこの 3 つの基準日で、提出日とは 2 週間から 1 か月ずれる） | 様式 220 が月次で出している取得枠の中身。**carry を「これから受け取る現金」として narrative に書いてよいかは、ここで決まる**。`buyback_remaining_share_ratio` は決議株式数のうちまだ買っていない割合で、0 に近ければ枠は使い切られており、`recent_filing` でも forward の還元は無い（6088 は 2026-08-05 提出＝齢 0 日だが、残枠 11%・取得期間は 7/31 満了）。`buyback_trailing_3m_acquired_ratio` は直近 3 報告月の取得株数 ÷ 発行済で、**carry の trailing 株数変化にまだ現れていない取得を拾う**（枠を持つ 365 銘柄のうち 219 銘柄は carry ≤ 0 だった）。`buyback_authorization_window_end` が as-of より前なら、直近の提出があっても枠はもう無い。**`null` は「読めなかった」であって「残っていない」ではない** — 様式の記載形式は filer ごとに揺れ、実測で残枠が読めたのは 8 割である。単独で自動除外・rank 変更に使わない |
   | `forecast_full_year_loss`（`event_warnings` / `risk_tags`。candidate metrics は `forecast_full_year_loss_flag`） | 会社自身が通期の経常または当期純利益を赤字で予想している。**この行の FV アンカーは自己履歴 PBR だけになっている**（赤字予想は forecast EPS を負にして forward PER を落とす）ので、implied upside は「黒字だった時代の倍率へ戻る」前提を含む。一過性（引当・減損）か構造的な縮小かを一次開示で切り分け、後者なら upside を額面で受けない。**単独で自動除外にしない** — 機械は両者を区別しないので判定は research が持つ |
   | 直近 cycle で棄却済み（`bargain_assessment` の reject / defer lane） | 深掘りを終えた ticker が翌 cycle も上位へ戻るのは E[r] 主キーの正常な挙動だが、**新しい材料が無いまま research 枠を再消費しない**。`baibai_engine.research_watch` の `rows[].thesis_fair_value_yen` と当日終値、`triggered`、直近開示を突き合わせ、(a) 価格が研究 FV を下回った (b) 新規の material 開示がある (c) 前回の unknown 軸を解消する決算が出た のいずれも無ければ既定で rejected（`reject_class: event_wait`）とし、reason に前回結論の日付と再評価 trigger を書く。selected にするなら**前回結論から何が変わったか**を narrative に明記する |
   | E[r] 履歴帯（較正 quintile 文脈） | 帯の記述統計としてのみ参照。個別銘柄の予測として書かない |
   | `data_quality_flags` / `durability_warnings` | flag が upside / downside をどちら向きに歪めるかを narrative に書く |
   | `stale_fin_flag` / `fin_latest_disclosed_date` | `true` は「予定日が過ぎたのにその開示が機械行に無い」。延期・決算期変更・provider 欠落を**一次開示で切り分けてから** narrative を書き、切り分け前の数字のまま selected にしない。`null` は判定材料が無いという意味で、`false`（照合して一致）と読み替えない |
   | `tse_capital_policy_status` / `tse_capital_policy_updated_on`（`selection.yaml` の `longlist[].capital_control` / `recommendations[]`） | 東証「資本コストや株価を意識した経営」要請への開示状況。**as-of の値ではなく月末断面である** — 東証は月末時点の一覧を翌月以降に公表するので、この値は as-of より 1〜2 か月前の状態で、直近に開示へ移った銘柄はまだ反映されていない（月次取込が止まればさらに古くなる。採用した断面の月は `refresh-capital-control` の `tse_sheets` で確認する）。開示率は Prime 94% / Standard 56% なので**開示済であること自体は弁別しない**。読むのは `tse_capital_policy_updated_on` の側で、アップデートが古いまま止まっているか、直近に更新されたかが、還元・資本配分の議論が動いているかの手がかりになる。`considering` は要請に対して検討中と申告している状態、`none` は as-of 以前の最新月次一覧に載っていない状態（対象外市場を含む）、`null` は参照できる月次スナップショットが無い状態で、`none` と読み替えない。**単独で自動除外・rank 変更に使わない** |
   | `large_holding_event_recent` / `large_holding_event_latest_on`（同じく `capital_control`） | 対象会社として直近 183 日に大量保有報告書系の提出があったか。株主構成が動いている文脈であり、**保有者が誰でどんな意図かは EDINET 本文を読むまで分からない**（純投資の機関投資家の変更報告が大半である）。`true` を活性化の証拠として narrative に書くなら、一次資料で提出者と保有目的を確認してからにする。`null` は観測窓が埋まっていない・この銘柄の EDINET 素性が過去の提出から分からない・対象会社を名指さない提出が窓にある、のいずれかで「言えない」状態であり、`false`（窓を観測して提出なし）と読み替えない。**単独で自動除外・rank 変更に使わない** |
   | `tender_offer_event_recent` / `tender_offer_event_latest_on`（同じく `capital_control`） | 対象会社として直近 183 日に公開買付書類系の提出があったか。`true` は**進行中または直近に終わった公開買付けがある**ということで、市場価格が買付価格に張り付いている可能性を含む。この場合 implied upside は割安ではなく買収対価の残差なので、FV convergence warning と併せて必ず一次開示（届出書・意見表明報告書）へ戻る。`null` の意味は上の行と同じで、`false` と読み替えない。**単独で自動除外・rank 変更に使わない** |
   | `next_earnings_status` | `announced` = 予定日を過ぎている。`scheduled` = 予定日が先。ただし前倒し開示した銘柄もカレンダーが更新されるまで `scheduled` に見えるので、`fin_latest_disclosed_date` が予定日の直前なら一次開示で確認する。`estimated` は推定日なので event risk 判定に使わず着手順の目安に留める。`unknown` は次回時期が不明 |

8. **OP3 深度契約**: selected 各銘柄について次を 1 項目ずつ機械的に突合する（印象で「満たしているはず」としない）。

   1. `upside` から希望的前提を剥がしても現行事業の正常化だけで期待値が正か
   2. 利益がピーク外挿でなく複数期レンジの正常利益か（循環のどこにいるかを書く）
   3. `catalyst` が日付または特定可能な event か（dated なら `catalyst_date`。undated なら再評価の観測条件を書く）
   4. 深掘り〜保有初期の dated event（決算・guidance・規制・macro monitoring）を消化したか
   5. リスク調整後に現金保有へ勝るか。net cash / 簿価を床にする銘柄は還元機構を確認したか
   6. carry 支配型で、`forecast_special_gain_flag`・FCF の配当カバーを剥がしても成立するか。carry の 2 成分は機械行でなく**一次開示で確定する**（機械の DPS / share-change は基準年ズレと遅れの両方を持つ）:
      - **配当**: 短信の配当表から特別配当を差し引いた普通配当が反復分。前期実績も同じ処理をしてから比較する（機械の `dps_actual_annual` は分割調整済みの前々期を指していることがある。前期比の跳ねを見る前に、両期が同じ基準か確かめる）
      - **buyback**: 取得枠の開示を読み、①**取得目的**（株式報酬・持株会向けは再放出されるので還元でない）②消却の明言 ③期間と残枠 を確認する。ToSTNeT は取得の場であって一回性の証拠ではない — 背後に取締役会決議の枠があるかで判断する。`net_share_change_yoy` は前年同期比なので、完了済み枠を carry に残す一方で執行中の新枠を取りこぼす — 過大・過小の両方向に外れる
   7. `rank` が機械 E[r] 降順から乖離する銘柄は理由を書いたか
   8. macro connection の research hint / sizing caution / estimate_caveats / bargain_topography のうち該当分を消化したか（該当なしの判断も書く）
   9. rejected 全件に具体的理由と `reject_class`（disposition_reason が正本、class は集計専用）

9. **publish**: [`assets/draft-template.yaml`](./assets/draft-template.yaml) を写して記入し、source `selection_id` へ束縛して `uv run baibai-engine screening shortlist publish <draft>`。publisher が longlist 行（rank・FV アンカー・参考価格・warning）を entry へ焼き込むので、run が prune された後もレビュー面が判断根拠を読める。selected に入れるのは **narrative を書ける entry だけ**で、件数の下限は無い。基準を下げて枠を埋めない（selected 0 件も正常で、その cycle は shortlist が正本判断になり session をここで complete する）。rejected を含む entries は longlist 全件で可。draft に `er_annual` を書かない（publisher が bound run から焼き込む）。
10. **検証**: publish された全 entry の焼き込み E[r] を bound run と機械照合する。stderr の follow-up 提案は、`fin_latest_disclosed_date >= event_date` の event と publish 時点より過去の due を出さない。消化済み event は次の公表済み日程、次いで将来の estimated date、どちらも無ければ undated condition へ進む。日付付きの提案だけを確認して `task add` で起票し、undated condition は次回日程の公表または新規 material 開示時に再評価する。
11. **cloud 反映**: `batch/scripts/r2_transfer.sh push-app` → `gh workflow run cloud-materialize` → run の completed success を確認。
12. **checkpoint と報告**: selected が 1 件以上なら session checkpoint を更新し、レビュー面（`/stocks/shortlist`）へ誘導する報告を出す。各 ticker に TradingView link（`https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A<code>`）を付け、機械順位との乖離・残 risk を明記する。人間の選択を待つ（session は active のまま `research` skill へ）。selected 0 件なら、人間確認を `not applicable` で偽装せず、published shortlist artifact（`kind: shortlist`、`selected_count: 0`）と `canonical_refs`、`completion_reason: no-shortlist-selection`、`result`、`next` を持つ final payload で opportunity session を complete する。`human_confirmation` は省略する。

## 既知の gotcha

- run store は 3 世代 retention。selection output のローカルファイルを消しても `screening selection show --selection-id <ID>` で読み直せる（bound run の evict 後も取れる）。
- 同じ as-of を作り直すと 3 世代を食い潰して前 as-of の run が消え、差分の前回側が空になる（前回候補上限の cap も効かなくなる）。`batch/scripts/r2_transfer.sh pull-longlist-history <DIR>` で永続 record を取り、手順 3 の select へ `--longlist-history-dir <DIR>` を渡すと前回側を復元できる（ローカル R2 token は serving bucket を読める）。record が無い日は前回 shortlist（application DB 永続）との比較が fallback。
- `operation checkpoint` の `--payload` は **JSON ファイルのパス**を取る（JSON 文字列を直接渡すとファイル名として解釈され失敗する）。
- `select` の再実行は**新しい selection を publish する**（冪等でない）。既存 selection の再取得には使わない。
- machine recommendation を shortlist と呼ばない。review 済み draft の publish だけが shortlist である。

## 参照

- 深度契約の背景・screening 判断境界: [`docs/reference/screening-runtime.md`](../../../docs/reference/screening-runtime.md)
- 資金・注文額 baseline: [`docs/portfolio-management.md`](../../../docs/portfolio-management.md)
