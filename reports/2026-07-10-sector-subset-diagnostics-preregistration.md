# sector subset diagnostics 事前登録（#321）

本レポートは #321 の `calibration-evaluate` sector subset diagnostics 追加について、実装前に目的・仮説・採否基準・検証予定を固定する。git history 上の本節 commit を事前登録の正本とし、実装後の採否判定は後続の PR / report で行う。

## 0. 目的と盲検性の限定

目的は、#309 の採否判定で一時 Python 集計として手作業した金融 sector subset 診断を、`calibration-evaluate` の再現可能な CLI 出力へ移すことである。今後の design / confirm 採否表では、同じ YAML 出力から subset coverage・rank IC・top-decile trap・全母集団との差分を転記できる状態にする。

盲検性の限定: 金融 subset の 6m 診断値は、#309 と `reports/2026-07-09-universe-er-population-preregistration.md` §6 で既知である。本検証は投資判断ルールの採否を新しく決めるものではない。新規性は、既知の手作業集計を production CLI の任意診断として再現し、今後の改善ループで同じ指標を機械的に出せるようにする点にある。

既知の比較対象は以下で固定する。

| window | cohorts | mean financial n | mean financial rank IC | IC positive share | financial top decile trap | all E[r] best decile trap | trap delta |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| design | 22 | 90.3 | 0.1196 | 0.8636 | 0.0202 | 0.0584 | -3.818pt |
| confirm | 18 | 104.3 | 0.0212 | 0.6667 | 0.0351 | 0.0578 | -2.271pt |

## 1. 実装仮説

`calibration-evaluate` に sector subset 指定を optional に追加する。指定がない場合、既存の YAML 出力と `results[*].aggregate` の形は変えない。指定がある場合だけ、各 result に subset 診断の optional section を追加する。

subset 診断は、指定 sector 群と指定 axis に対して以下を cohort と aggregate に出す。

1. subset coverage: cohort 数、subset 銘柄数、mean subset n。
2. subset rank IC: `er_annual` など指定 axis と realized return の rank IC、aggregate では mean rank IC と IC positive share。
3. subset best-decile trap: subset 内の同 axis best decile の trap rate。
4. all-population best-decile trap: 全母集団の同 axis best decile trap rate。
5. trap delta: subset best-decile trap から all-population best-decile trap を引いた差分。

金融 sector は、#309 と同じく銀行業 / 証券・商品先物取引業 / 保険業 / その他金融業を対象にする。axis は 6m 採否表との対応を保つため `er_annual` を主対象にする。

## 2. 採用基準

以下をすべて満たす場合だけ採用する。

1. sector subset 指定なしの既存 `calibration-evaluate` 出力が後方互換で変わらない。
2. focused tests が通る。
3. 既存 calibration store で design / confirm 6m の金融 sector 診断を出力できる。
4. 出力された aggregate が、#309 report §6 の手作業値と丸め許容内で一致する。対象は mean financial n、mean financial rank IC、IC positive share、financial top decile trap、all E[r] best decile trap、trap delta とする。
5. full gates が通る: `uv run baibai-loop-validation`、`uv run ruff format --check .`、`uv run ruff check .`、`uv run mypy`、`uv run pytest`。

丸め許容は、表示桁への丸め差だけを許す。採否に影響する単位のズレ、母集団定義のズレ、trap delta の符号反転は許容しない。

## 3. 検証予定コマンド

引数名は実装時の最終 CLI 形に合わせる。事前登録時点では、sector subset と axis を任意指定できる仮名として以下を置く。

```bash
uv run baibai-loop-screening calibration-evaluate \
  --calibration-dir data/screening/calibration-universe-er-population \
  --start 2022-09-01 \
  --end 2024-06-30 \
  --horizon 6m \
  --sector-subset financial \
  --sector-subset-axis er_annual \
  --out .cache/sector-subset-financial-design-6m.yaml

uv run baibai-loop-screening calibration-evaluate \
  --calibration-dir data/screening/calibration-universe-er-population \
  --start 2024-07-01 \
  --end 2026-06-30 \
  --horizon 6m \
  --sector-subset financial \
  --sector-subset-axis er_annual \
  --out .cache/sector-subset-financial-confirm-6m.yaml
```

後方互換確認では、同じ `--start` / `--end` / `--horizon` / `--calibration-dir` を sector subset 指定なしで実行し、既存 YAML と差分がないことを確認する。

## 4. 不採用条件 / 残リスク

以下のいずれかに該当する場合は不採用とする。

1. sector subset 指定なしの YAML が既存用途を壊す。
2. `results[*].aggregate` の既存 key・意味・型が変わる。
3. 金融 sector 診断が #309 report §6 の手作業値を再現できない。
4. subset の母集団定義、axis、trap 分母が曖昧なまま出力される。
5. focused tests または full gates が通らない。

残リスクは、sector 名・業種コードの local data 表記揺れにより、金融 subset の membership が #309 の手作業集計とずれることである。この場合は CLI の引数または出力 metadata に、使った sector 定義を明示する必要がある。
