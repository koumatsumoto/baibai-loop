# Issue #1580: materializeの有限CI計測

2026-10-23の準備記録。baseは`c30cffc2eada4a951e236818faf172c45c933cda`。
本番高速化は未実装、実測値もまだない。追加は[script](../../../tools/diagnostics/measure_materialize.py)
1本、[軽いtest](../../../tests/tools/test_measure_materialize.py)、[手動CI](../../../.github/workflows/measure-materialize.yml)、本記録。
既存の`master` / `screening_sqlite` / `screening_run` helperを再利用し、常設fixtureを増やさない。
`screening_run`のsentinel分岐には型だけのcastを足し、toolsからの型付きimportでも既存の入力意味を保つ。

共有されたrun `39945826057`は全体24m02s、hydrate 5m21s、read-model生成12m58s、
serving upload 4m34s。旧`fc28`のrun `39928864247`は25m48sだが、同一入力・環境の比較ではなく、
回帰とは認定しない。この計測はhydrate/uploadを対象にしない。

## 測定対象

現行`export_read_models`を呼び、既存CachedScreening/Ledger/Research、PreparedSecurityInputs、
PreparedMarketContextをそのまま使う。候補はper-ticker長期価格・外部評価の接続に伴う実schema
shape照合と、sectorのexact端点ごとのpeer return/median反復。根拠は
[schema](../../../engine/src/baibai_engine/storage/observations/schema.py)、
[価格読取](../../../engine/src/baibai_engine/market_context/prices.py)、
[sector計算](../../../engine/src/baibai_engine/market_context/security.py)、
[外部評価読取](../../../engine/src/baibai_engine/external_assessment/reader.py)。
価格/外部評価のprimary key・indexesは存在する。table metadata照合とdata走査を区別し、
「全table data scan」とは断定しない。元関数へdelegateし、実接続のschema/hydration、原本・
publicationの読取検査を省略しない。本番code、required CIの閾値・worker数は変更しない。

fixture生成と計測は別process。生成wall/CPU/peak RSS・入力hash/bytesと、export各段・securityの
外部評価/価格/相対/sector、SQLite connect/schema照合/medianのwall/CPU/call数を分ける。
時間単位はns、inclusive時間は入れ子なので合算しない。SQLite/実schema照合はcaller別回数も出す。
peer端点は回数のみ。現行_sector_blockはmedian成立時だけown端点を1回計算するため、peer試行数は
sector内端点呼出数−sector median数。この呼出契約を変える将来修正では計測器もreviewする。
Linuxのexport直後peak RSS（KiB）と出力files/bytesも記録する。hash/oracleはexport計測の後に行う。
観測器のoverheadを含む。fresh Python processだがOS page cacheは生成で温まるためcold diskではない。
寄与の小さい段階は最適化を不採用にできる。検査省略は改善案にしない。

## 有限入力・意味確認

750平日bar、benchmark 1321、各銘柄2時点のsynthetic model valuation、Screening 1runだけを作る。
application/経済storeは空の現行schema。価格は銘柄ごとに傾きの異なる一次式で、1銘柄は通常の
20bar比較開始日、別の1銘柄は最終日を欠く。重い原本archive・実ledger/Research履歴・財務原本・
本番データは持ち込まない。その領域の寄与や本番12m58s全体の再現は主張できない。

| case | N | sector数 |
| --- | ---: | ---: |
| `small-concentrated` | 8 | 1 |
| `medium-concentrated` / `medium-dispersed` | 256 | 1 / 33 |
| `upper-concentrated` / `upper-dispersed` | 4000 | 1 / 33 |
| `bounded-concentrated` / `bounded-dispersed` | 1000 | 1 / 33 |

4000件は価格約300万行、集中sectorのpeer候補は約1600万回。予算適否は未実測。
超過時は約75万行・100万回の1000件caseへ縮め、4000件の実測成功や線形外挿を主張しない。
33業種分散は均等配置で、実際の業種構成の推計ではない。

小さい独立oracleは先頭3銘柄のprice/20bar return、exact端点、自分・欠損peerの除外、手計算median、
外部評価の最新/前回を直接JSONと照合し、productionの算術helperを使わない。
全出力の相対path＋raw hashと、key順だけ正規化したsemantic JSON digestを記録する。
生成時刻/cutoffは`2026-10-20T09:00:00Z`、保存run時刻/revision IDも固定し、`generated_at`を消さない。
小ケースだけ無計測exportとの全出力同一性も確認する。将来は同recipe・入力hash・clock・caseと
同等runner/Python/SQLite/lockでbefore/afterを比較し、oracleとdigestの差も確認する。

## Review後のCI手順・budget

既存`ci.yml`はPython quality jobにsetupを共有するが、長い測定は通常testへ追加しない。
本番`cloud-materialize.yml`はR2/leaseを使うため変更しない。主担当が統合・review後、手動CIで
小ケース1回→中規模集中/分散各1回→必要なら4000件分散/集中各1回の順に選ぶ。

```bash
gh workflow run measure-materialize.yml --repo koumatsumoto/baibai-loop \
  --ref <REVIEWED_REF> -f case=small-concentrated
```

一dispatch=一case/runner、job上限20分、fixture 240秒、計測720秒、TERM後15秒でkill。
初回は小ケースのみ。最初の5caseで最大100 runner-minutes、代替2case込みで最大140分の上限。
予算超過はpartial profile/終了状態を読み、1000件の対応caseへ進む。恒久elapsed閾値は追加しない。
`status=measured`、oracle、小ケースの`observer_output_parity=passed`を確認し、最後のstep log/run summaryの
`profile.json`をrun URL/commitと共に保存する。新しいartifact Actionは加えない。

本番environment/secret、source取得、R2/httpfs、Node/frontend、scheduleは使わない。
依存setup後は`UV_OFFLINE=true`、scriptのaudit hookはnetwork/DNS/subprocessを拒否する。
生成先は`RUNNER_TEMP`だけ。計測判断後に一時workflowを撤去する。
ローカルtest/lint/type/build/benchmark/application CLI・fixture生成、CI起動、PR作成は未実施。
静的読取とgit差分確認のみで、検証成功・実測済みとは報告しない。
