# FV convergence warning 事前登録

価値tier: T1〜T3 — FVへ収束してreversion余地を失ったcarry主導候補をOP3前に表面化し、人間の反復確認を減らす。

## 仮説と固定する判定

selection longlistの候補について、次を全て満たす場合だけ`price_at_or_above_all_fv_anchors` warningを付ける。

1. screening参考価格`market_price_yen`が有限かつ正である。
2. `fv_sector_median_yen` / `fv_self_range_yen`のうち、有限かつ正のanchorが1本以上ある。
3. 参考価格が利用可能な全anchor以上である。anchorが1本ならその1本、2本なら両方を対象にし、等値を含む。
4. `er_reversion_annual`が有限で0以下である。

anchorが0本、価格が無効、またはreversionが欠損・未知・非数値・非有限なら`not_evaluable`とする。anchorの片方だけが有効なら有効な1本で評価し、無効値を0へ補完しない。2本のうち片方だけを上回る場合と、reversionが正の場合は`clear`とする。

payloadには`status`、warning code、利用した参考価格、anchor名と値、`er_reversion_annual`を保存する。annotationはlonglistとBaibai Appのshortlist review面に表示するだけで、candidate、screen pass、E[r]、rank、recommendation集合を変えない。

## 事後に記録する記述計測

実装前には結果を計測しない。実装後、[`2026-08-02-reject-class-retrospective.md`](../2026-08-02-reject-class-retrospective/report.md)で`price_already_converged`に分類されたOP3 rejected 12 ticker-eventを分母にし、次を同じreportへ追記する。

- `warning` / `clear` / `not_evaluable`の件数と、`warning / 12`のcoverage。
- 同じ3 shortlist cycleのselected ticker-eventを分母にしたwarning件数と率。これは過剰warningの記述値であり、selectedを正例とは仮定しない。
- 各`not_evaluable`の欠損入力と、warningにならなかった既知の`price_already_converged`事例の理由。

これは非ランダムな少数の遡及標本なので、精度・再現率・因果効果・統計的優位は主張しない。採用理由は、最多の棄却型に既存入力だけで低コストの調査annotationを付け、判断とrankingを人間側に残すことにある。

## 検証契約

- 1 anchor / 2 anchors、等値、片方だけ超過、anchor欠損、価格欠損、reversion欠損をunit testで固定する。
- bool、文字列、NaN、無限大、0以下の価格・anchorを判定入力に使わないnegative testを置く。
- warning有無だけを変えた同一candidate集合で、rank、E[r]、screen結果、recommendation ticker列が不変であることを固定する。
- read modelが旧payloadを`not_evaluable`として読めることと、UIがwarning / 判定不能を区別して表示することを確認する。

## 遡及結果

事前登録commit `019949b`の後に計測した。入力は次の3 selection artifactと、selected / rejectedラベルの正本であるapplication DBを使った。

| source | SHA-256 |
| --- | --- |
| `.cache/opportunity/2026-07-17/selection.yaml` | `c311d199d0fc744e47cd0df6aae3eda870781baeae70ae41a70311a90200e7df` |
| `.cache/opportunity/2026-07-28/selection.yaml` | `64c864d869f57c6a413e57767cca99fa731350e3f2df3ddf76e4081392ea8faf` |
| `.cache/opportunity/2026-07-29/selection.yaml` | `b01948dcad79a5922c0b693a53657f2c964dba186bcd97b047ebfb06d8826fe2` |
| `stores/application/baibai.sqlite` | `d99b258ed4cb4c763d9bf154d792aef092618e2627d2e6afce4b1c665c5caa62` |

| cohort | warning | clear | not_evaluable | warning率 |
| --- | ---: | ---: | ---: | ---: |
| `price_already_converged` rejected | 2 | 10 | 0 | 2 / 12 = **16.7%** |
| 同じ3 cycleのselected | 1 | 23 | 0 | 1 / 24 = **4.2%** |

rejectedでwarningになったのは2026-07-17の7575と2026-07-29の7944で、どちらも現値がsector / self-rangeの両anchor以上かつreversionが負だった。clear 10件のうち9件は現値が片方のanchorだけを上回り、もう片方には上値余地が残った。残る2026-07-28の2267は現値が両anchor未満でreversionも正だった。`not_evaluable`は無く、欠損補完は発生していない。

selectedのwarningは2026-07-29の7575だけだった。この記述比較はwarningの正誤を確定しないが、3 cycleのselectedの95.8%をwarningにしないため、OP3面を広い注意表示で埋めない。既知classのcoverageは16.7%に限定されるので、warningは「人間の棄却型を代行する検出器」ではなく、両machine anchorへの収束を高確度で表面化するannotationとして扱う。
