---
title: "Candidate shortlist report"
summary: "opportunity path OP3の人間レビューgateに出す候補HTMLレポートの生成方式とnarrativesスキーマ。"
doc_type: reference
status: active
last_reviewed: 2026-07-15
---

# candidate-report — 候補shortlistレポートの生成

`opportunity` pathの[OP3](../operations/decision-cycle.md#opportunity-path)で、人間がprimary-research setを選ぶための候補HTMLレポートを生成する。候補件数はOP3の契約を正本とする。1銘柄へ先に決め打ちせず、比較可能な候補群を先に人間へ渡す人間レビューgateの成果物。

## 設計

packet-scaffold と同じく「機械 = data plumbing / 人間 = judgment」。レンダラは screening 出力（`selection-output.yaml` の `audit_pool` と`candidates.yaml`のmetrics）とprepare出力（workspaceの`selection.yaml`）から以下を機械取得し、**数値を転記しない**。

- 価格・valuation・自己資本比率・net cash・OCF/FCF・配当basis: screening評価用参考価格での基本fact。この価格はscreeningの入力整合用で、発注に使うJPX raw/unadjusted closeではない。発注価格はprimary research後の`plan-limit`で別に取得する。
- 機械E[r]と**reversion/carry分解**: carry偏重のE[r]は割安の証拠にならないため、合計値だけでなく内訳を第1層に出す。
- **FVアンカー構成**（`fv_self_range_yen` / `fv_sector_median_yen`と使用anchor metrics）と乖離: 自社レンジ比の安さと業種比の安さはmispricing仮説も失敗モードも異なるため分けて示す。
- **値位置**（60日変化・52週安値からの位置）: 新鮮なdislocationか慢性的な安値放置かは一時的/構造的の事前判断を変える。
- **売上・営業利益YoY**: narrativeの増収減益等の主張の隣に機械値を置く。
- **流動性**（日次売買代金・liquidity_status）: 比較固定順の第4軸「購入可能性」の機械入力。
- **`next_earnings_date`とevent warning**: research窓・注文窓のevent riskを深掘り選択の時点で見せる。`next_earnings_date: null` は JPX snapshot に既知日程がない（未定を含む）状態で、決算が存在しないという意味ではない。
- **データ品質flag**（TTM品質非exact・EDINET取得失敗・BS前期繰越・freshness warning）: stale・欠損データ上の指標を無警告で信じさせない。
- **portfolio annotation**（`unheld / held / reserved / held_and_reserved`）: prepare出力から機械join し、保有・予約状態を推定や固定文言で書かない。
- 入力 sha256（selection / candidates / prepared selection）。

運用者は各候補の定性 narrative だけを `narratives.yaml` に書く。

生成 HTML は `.cache` 配下の **ephemeral 成果物で commit しない**（screen とレンダラの再実行で再現する。records には promote 済み packet/review だけを残す方針と一致）。

## Weekly report の差分確認

直近の前回reportが確認できる週次runでは、前回と今回のhuman-review shortlistをticker集合で比較します。

- `new`: 今回だけに含まれるticker。現在のscreening結果と定性判断に基づき、今回shortlistへ入れる理由とnarrativeを新たに書く。narrativeを書く前に会社IR・TDnetの直近開示をタイトルレベルで確認し、screeningのas-of財務に反映されないmaterial開示（業績修正、資本政策、TOB/MBO、不祥事等）を`why` / `counter`へ反映する（[OP3](../operations/decision-cycle.md#opportunity-path)の開示スキャン契約）。前回reportを確認できないfull reportでは全候補にこの確認を適用する。
- `continued`: 前回と今回の両方に含まれるticker。前回narrativeは自動継承せず、audit-pool順位差、価格、前回as-of後に会社IR・TDnet・EDINETで公表された最新開示、最強countercaseとmaterial deltaを確認する。
- `exited`: 前回だけに含まれるticker。今回のaudit pool外である場合も含め、今回shortlistへ残さない現在の理由を新たに書く。

`continued`は、上記確認後も定性判断を支える根拠にmaterial changeがない場合だけ、前回narrativeを今回の`narratives.yaml`で再利用できます。material changeがあるfieldは現在の一次情報と判断へ更新し、確認不能なら再利用せずその不足を明記します。`new / continued / exited`、順位差、確認した最新開示、narrativeを再利用または更新した理由はoperation Issueのshortlist checkpointへ残し、今回reportと同時に人間へ提示します。前回reportを確認できない場合は分類を推定せず、全候補のnarrativeを確認するfull reportを作ります。

価格、valuation、配当basis、E[r]、FVアンカー乖離等の数値は、毎回そのrunの`selection-output.yaml`と`candidates.yaml`からrendererが生成します。前回HTMLの表示値や前回`narratives.yaml`に数値を転記して再利用しません。

この差分運用は当面、既存のrendererとnarratives schemaを変えずに実施します。operation Issue上の2〜3回の運用結果から、繰り返し必要になるfield、表示先、確認コストが安定した後にだけrenderer/schema変更を別のimprovement taskとして判断します。

## 生成手順

1. OP2 で `candidates.yaml` / `selection-output.yaml`（`--audit-top 20`）を作る。
2. audit poolから[OP3の件数契約](../operations/decision-cycle.md#opportunity-path)に従って候補を選び、[`tools/candidate_report/narratives-template.yaml`](../../tools/candidate_report/narratives-template.yaml)をrunのworkspaceへ複製して記入する。
3. 直近の前回reportがある場合は[`Weekly report の差分確認`](#weekly-report-の差分確認)を実施し、結果をoperation Issueへ記録する。前回reportがない場合はfull reportとして全narrativeを確認する。
4. レンダラを実行する。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python -m tools.candidate_report.render \
  --selection .cache/opportunity/YYYY-MM-DD/selection-output.yaml \
  --candidates .cache/opportunity/YYYY-MM-DD/candidates.yaml \
  --narratives .cache/opportunity/YYYY-MM-DD/narratives.yaml \
  --prepared .cache/opportunity/YYYY-MM-DD/selection.yaml \
  --out .cache/opportunity/YYYY-MM-DD/candidate-report.html
```

`--prepared`は`baibai-loop-opportunity prepare`が書くworkspaceの`selection.yaml`で、portfolio annotationの唯一の機械sourceとする。narrativeのtickerがprepared audit poolに無い場合、レンダラはerrorで停止する。

## narratives スキーマ

- `meta`: `title` / `target_session` / `order_by`（表示順の説明。推奨順位ではない）/ `intro_notes`（brief 直下の箇条書き。screening 修正や basis の注記）。
- `candidates`: 順序 = 表示順。各 `ticker` は当該 run の `audit_pool` に含まれること。`ploss` は `低 / 中低 / 中 / 要精査 / 高`。定性 key は `why`（なぜ安い）/ `temporary`（一時的か）/ `structural`（構造的か）/ `survive`（5年耐性）/ `unlock`（株主価値向上要因）/ `counter`（最強反対仮説）/ `research`（個別リサーチ確認事項）/ `value`（深掘り価値）/ `prov`（暫定判断）。`sector_label` は任意（未指定なら screen の `sector_33`）。
- `excluded`: audit pool で非選択の ticker と具体的理由。「順位が低い」「予算外」だけは不可。投資対象外もここへ理由付きで。

audit pool に無い ticker を narrative に書くとレンダラは error で停止する（存在しない数値を出さない）。

## Related

- [`../operations/decision-cycle.md`](../operations/decision-cycle.md)
- [`../workflow/research.md`](../workflow/research.md)
