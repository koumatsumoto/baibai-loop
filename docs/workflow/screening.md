---
title: "Workflow — screening"
summary: "割安 screening：全上場普通株を対象に、playbook 連動の screen で割安ゾーンを機械抽出して candidates の事実を出力し、select で research 候補を選り分ける。"
doc_type: workflow
status: active
last_reviewed: 2026-07-06
---

# Workflow — 割安 screening

単一ループ（[`../doctrine.md`](../doctrine.md) §2）の「お買い得を機械的に見つける」工程。全上場普通株を対象に **valuation ranking で割安ゾーンを機械抽出**して `records/02-candidates/` に事実を出力し、`select` で深掘りする候補を選り分ける。この工程は決定論的な機械処理（L2）であり、出力は解釈を含まない **事実**。指標算出の仕様は [`../reference/valuation-metrics.md`](../reference/valuation-metrics.md)、CLI / SQLite の実装は [`../reference/screening-runtime.md`](../reference/screening-runtime.md)、契約の正本は `records/_schemas/candidates.json`。

## Universe（対象範囲）

screen の評価対象（scope）は **全上場普通株** とし、規模・流動性・上場期間・規制 flag は除外条件ではなく **candidates に記録される事実** として扱う。

| 条件 | 値 | 理由 |
| --- | --- | --- |
| 銘柄種別 | 普通株のみ（ETF / REIT / 優先株を除外） | 事業会社の valuation 判定が対象 |
| 上場市場 | プライム / スタンダード / グロース（TOKYO PRO Market は対象外） | 一般投資家が取引可能 |
| bar 履歴 | 直近 20 営業日以上 | 売買代金・自己レンジの事実算出に必要な最小データ |

research 候補への絞り込み（時価総額・売買代金・上場期間・JPX 規制）は、**分析層のパラメータ `selection.liquidity`**（既定: 時価総額 100 億円以上・20 営業日平均売買代金 1 億円以上・上場 182 日以上・JPX 規制銘柄の除外）として `select` の時点で適用する。screen の段階でデータを狭めない（どの銘柄も screening の事実を持つ）ことで、絞り込みの条件を目に見える・変更できる形に保つ。除外に使う JPX flag は重篤な 4 種（特別注意銘柄・整理銘柄・取引停止・上場廃止警告、`records/_config/screening-rules/` の `required_jpx_flags`）のみ。信用規制の日々公表のような軽度の flag では除外せず、candidates の事実として記録したうえで research 側の需給・流動性リスクとして扱う。

candidates YAML（`records/02-candidates/`）は market.sqlite から再生成可能な機械出力として local store に置き git に積まないが、`select` の前回比較・`ticker-profile` の直近記録参照が読むため、market.sqlite と同様にローカル backup の対象にする。

**valuation 比較の母集団**（sector / 市場中央値）は `selection.liquidity` を満たす流動性母集団に固定し、小型・低流動性銘柄の混入で判定が歪まないようにする。

## 割安の判定軸

各銘柄について、[`../reference/valuation-metrics.md`](../reference/valuation-metrics.md) の指標（PER forward/trailing・PBR・EV/EBITDA・P/S・PCFR・OCF yield・FCF yield・net-cash ratio）を、**業種中央値相対** と **過去自己レンジ（直近 750 営業日 ≒ 3 年、上場 3 年未満は上場来）相対** の percentile として出す。スコアは軸別の座標であり、単一の合成点や売買指示には畳まない（[`../doctrine.md`](../doctrine.md) 柱 5）。

## Playbook-linked screen（OR 条件、最低 1 つ）

以下の割安 screen のうち **最低 1 つ** を満たす銘柄を通過とする。閾値は `records/_config/screening-rules/*.yaml` を正本とする。複数 hit は research 優先度を上げる材料。`evidence_hits[]` に playbook 名・hit reasons・判定 metrics を記録する。

- **`valuation-reversion`**：PER / PBR / 正の EV/EBITDA が業種中央値との比較・過去の自己レンジの下位にあり割安（条件 A）。あるいは自己レンジからの σギャップが大きく（valuation の統計的な割安）、かつ業績悪化ゲートに触れない銘柄（条件 B）。60 営業日の下落は要件にしない（`price_change_60d` は事実として記録するが判定には使わない）。銀行・証券・保険・その他金融は除外（規制資本・与信サイクルの影響で、事業会社と同じ判定ができない）。
- **`cash-rich-asset-discount`**：現金性資産 / 時価総額・株価純資産倍率・自己資本比率で、現金や資産に対して割安な銘柄を拾う。EDINET のネットキャッシュとの突き合わせで矛盾を抑止する。営業赤字・営業利益の前年比急減（悪化ゲート）は除外。金融・電気ガス・卸売・不動産は除外（バランスシートの意味合いが事業会社と異なる）。
- **`cashflow-yield-discount`**：期間を正規化した直近 12 か月の営業キャッシュフロー利回り（OCF yield）で、現金創出力に対する割安を拾う。キャッシュフロー悪化（`cfo_yoy` の下限割れ）・営業利益の前年比急減・フリーキャッシュフローのマイナス（重設備型）は除外。金融・電気ガスは除外。
- **`sales-discount-growth`**：株価売上高倍率（P/S）が業種中央値より安く、売上成長が続いている銘柄。慢性的な赤字企業を弾くため営業利益率の下限を課す。金融は除外。

各 screen の金融などの除外根拠と悪化ゲートの詳細は `records/_config/screening-rules/*.yaml` と本 doc を正本とする。

## Selection lens（triage）

`select` は candidates と最新の macro context を突き合わせ、深掘りする候補を選り分ける。lens は割安ゾーンを狭めるためのものではなく、**塩漬け耐性と過去調査との重複**の観点で着手順位を付ける。

| lens | 目的 | 扱い |
| --- | --- | --- |
| `durability`（塩漬け耐性） | 長期保有に耐えるか（ネットキャッシュ・営業 CF 黒字・低負債・借換耐性・配当）を `high\|medium\|low\|unknown` で注記 | 採用の必須確認（[`../portfolio-management.md`](../portfolio-management.md) の耐性ゲート）に接続する入力。ranking には使わない |
| `prior_research` | 過去の research で deferred / rejected にした候補の再登場を抑え、同じ候補への偏りを下げる | `records/03-thesis/` の判断履歴から機械的に引く |

ranking の主キーは **valuation discount（割安度）** とする。組み込みの selection profile は `balanced` のみ。閾値を変えるときは `records/_config/screening-rules/` の設定を編集して `select` を再実行し、出力の差分を確認する。**短期の急落銘柄を上位に押し上げる仕組みや、リスクオン相場で逆張り候補を沈める仕組みは持たない**（保有期間ではなく valuation と耐性で判断するため）。

`select` の triage は `records/_config/screening-rules/*.yaml` の `selection` block を契約とする（閾値 baseline の正本は [`../reference/screening-runtime.md`](../reference/screening-runtime.md) §8）。

- `selection.default_profile`：明示 `--profile` がないときの built-in profile。built-in は `balanced` のみで、未知 profile は rules load 時に error にする。
- `selection.liquidity`：research 推奨に適用する規模・流動性・上場期間・JPX 規制の絞り込み。screen の scope は全普通株のままで、絞り込みはこの分析層パラメータだけが担う。
- `selection.durability`：塩漬け耐性の閾値。built-in profile はコード側の閾値を優先し、この YAML block は load-time contract と custom profile のベースとして扱う。
- `selection.diversity`：recommendations の sector / playbook 集中度、過去 candidates の混入上限、previous overlap warning。

## Candidates 出力（事実）

```text
records/02-candidates/YYYY/MM/YYYY-MM-DD.yaml
```

1 回の実行 = 1 ファイル（週次で運用）。candidates は L1 SQLite から決定論的に導かれる L2 出力であり、**git に積まないローカル保存**（`.gitignore` 対象）とする。契約の正本は `records/_schemas/candidates.json`。中心となる field は ticker / name / sector_33 / valuation 指標 / `metrics`（フラットな派生値）/ `ttm_quality` / `evidence_hits[]`（通過した screen・該当理由・判定に使った指標値）。`universe_size` に対象範囲（全普通株 + bar 履歴条件）の銘柄数を記録し、母集団の確認に使う。欠損値は `null` で明示し、単一の総合スコアは持たせない。

## 実行

コマンド列（`bootstrap-cache` → `extract-edinet-metrics` → `verify-cache-coverage` → `run` → `select`）の e2e 導線は [`../operations/monthly-cycle.md`](../operations/monthly-cycle.md) §2–3、CLI 引数 / env / SQLite schema の実装仕様は [`../reference/screening-runtime.md`](../reference/screening-runtime.md) を正本にする。ここでは工程の意味だけを記す。

`run` は開始時に SQLite のデータ充足を検証し、不足があれば即座に失敗させる（provider API へはフォールバックしない）。JPX 規制情報と EDINET の前処理済み指標は必須入力。`select` の推奨順位は**機械 E[r]（成分分解付き年率見積り）の降順**を主キーにする（E[r] 欠損は後置・従キーは playbook 優先順 + 強度キー。採用根拠は較正リプレイの design/confirm 検証）。macro context の `sector_tilts` は追い風 / 向かい風の参考情報として使い（機械的な足切りにはしない）、`recommendations` と `selection.diagnostics` を出力する。`--macro-context` を省略すると `records/01-macro-context/` の最新 context を自動解決する（`valid_until` が asof より古い context は鮮度切れとして失敗させる）。

## 長期予測力の計測（estimate calibration）

screen の軸・閾値・select 順位が「3 か月以上先の割安回復」を予測できているかは、`calibration-build` / `calibration-evaluate` の較正リプレイで計測する（[`../reference/estimate-calibration.md`](../reference/estimate-calibration.md)）。ランキング・ゲート・閾値の改訂は、事前登録した仮説をこの計測で design/confirm 分割の両方で確認した場合だけ行う（doctrine 柱 5 の誠実性規律）。

## 事実と分析の分離

candidates は **事実層**。閾値の適用と screen の該当判定は機械的であり、「なぜ割安なのか」「採用すべきか」の解釈は [`./research.md`](./research.md) 側で行う（[`../doctrine.md#fact-analysis-separation`](../doctrine.md#fact-analysis-separation)）。candidates 本文には因果・予測・相場観を書かない。

## 参考

- [`../reference/valuation-metrics.md`](../reference/valuation-metrics.md)：指標算出仕様
- [`../reference/estimate-calibration.md`](../reference/estimate-calibration.md)：長期見積り較正リプレイ
- [`../reference/screening-runtime.md`](../reference/screening-runtime.md)：CLI / provider / SQLite schema
- [`./macro.md`](./macro.md)：select が使う sector_tilts
- [`./research.md`](./research.md)：candidates を起点にした個別調査
- [`./playbooks.md`](./playbooks.md)：割安 value の archetype
