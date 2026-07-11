# vNext 意思決定 walking skeleton 検証

## 検証の位置づけ

2026-07-03 時点の 2331 ALSOK 記録を使い、候補分析から人間判断の記録までを
オフラインで履歴再生する。現在の株価や企業情報を更新した購入推奨ではない。
bid / ask は当時の実測板ではなく、記録済み終値の周辺に置いた合成 fixture であり、
canonical packet でも `synthetic_fixture`、`historical`、`is_executable: false` とする。

実行コマンド:

```bash
uv run python tools/vnext_spike/decision_packet.py \
  --input tests/fixtures/vnext-walking-skeleton/input.yaml \
  --review tests/fixtures/vnext-walking-skeleton/review.yaml \
  --decision tests/fixtures/vnext-walking-skeleton/decision.yaml \
  --output /tmp/vnext-decision.json
```

このコードは正式な product API ではない。後続 Issue #330、#331、#334 で portfolio、
decision packet、価格方針の責務を確定したら削除する disposable spike とする。

## 検証した境界

処理を次の4 artifactに分離した。

1. `input.yaml`: 人間判断前の候補、資金、合成 quote、見積り、リスク、source
2. analysis draft: 入力から生成する不変の提案 packet
3. `review.yaml`: analysis draft の SHA-256 に束縛した独立 second pass
4. `decision.yaml`: proposal と analysis draft の SHA-256 に束縛した過去の人間判断

数量、上限価格、基準日、ticker のいずれかが変わると proposal digest が変わり、過去の
`approve` は再利用できない。独立レビューも対象 draft が変わると失効する。今回の
`approve` は `historical_outcome` として current user decision の外側へ付与し、AIの現在提案には
しない。current decision は review ID と review digest にも束縛し、review より前の判断を拒否する。

## 第1層: 判断開始に必要な要約

| 項目 | 出力 |
| --- | --- |
| 候補 | 2331 ALSOK |
| 基準日 | 2026-07-03、`historical_replay` |
| packet status | `historical_replay_only`、非actionable |
| AI提案 | `defer_non_executable_replay` |
| 価格方針 | `shallow_limit`、上限 1,050 円、100 株 |
| 既存注文引当 | 8929の未約定注文 119,000 円 |
| 今回の予定額 | 105,000 円 |
| 今回約定後の利用可能資金 | 8,128,900 円 |
| 待機資金下限からの余裕 | 6,128,900 円 |
| 5年 base 総合 CAGR | 7.11% |
| 3年 sanity check 総合 CAGR | 8.87% |
| 既存評価 snapshot | FV 1,300 円、期待利回り 14.4%、RR 2.5 |
| 最大の反証要因 | NDC統合と資本集約性により、OCFが示すほどFCFが残らない可能性 |
| 記録済みの人間判断 | `approve`、ただし提案とは別の履歴 outcome |

最良売気配 1,053 円は上限を超えるため即時購入を選ばない。1,050 円の浅い指値は
待機資金下限を守り、1,023 円の深い指値は約定可能性を下げる価格優先案として比較する。
既存引当と今回の prospective notional は別に計算し、二重引当を隠さない。

## 第2層: 根拠と反証

3年・5年の各 horizon に bear / base / bull を置き、入口価格 1,050 円に対する期末株価と
累積配当から総合 CAGR を機械計算した。second pass は同じ6値を別 artifact から再入力し、
差分をコードで検出する。

| horizon | bear | base | bull |
| --- | ---: | ---: | ---: |
| 3年 | -2.54% | 8.87% | 15.55% |
| 5年 | -1.57% | 7.11% | 12.24% |

恒久損失は balance-sheet liquidity、market liquidity、debt repayment、cash-flow
conversion、dilution、customer concentration、structural decline、governance/accounting
の8軸を扱う。軸が欠けた場合は例外で分析を捨てず、`incomplete` packet と不足軸を返す。
今回は cash-flow conversion を `concern`、複数軸を `unknown` とし、一次情報確認も
`partially_verified`、代替候補比較も `unavailable` のため warning を明示する。これらは期待値を
踏まえて人間が引き受けられるため hard gate にしない。packet が非actionableなのは履歴再生だからである。

quote、資金、価格上限、scenario、最大リスク、旧FV/RR/期待利回り、集中度警告の各値は、
`origin`、`source_ids`、`derivation` を介して入力 source または計算式へ遡れる。source IDの
未登録、空参照、重複ID、存在しないローカルrefは生成時に拒否する。

## 不足情報

1. 追跡済み thesis に一次 IR URL が保存されていない。
2. 顧客集中度と governance/accounting の根拠が定量化されていない。
3. quote は合成履歴 fixture であり、現在の執行可能価格ではない。
4. 機会費用を独立評価する代替候補 packet がない。

一次情報不足と認識済みの恒久損失懸念は一律 hard gate にしない。current packet では warning
認識付きの人間判断を許す。一方、履歴quote、必須軸の欠落、未検証の算術、review が変更を
要求した旧提案、digest と一致しない人間判断を live-ready として扱うことは許可しない。

## 後続設計へ残すもの

- 第1層は候補、5年base、3年sanity、最大リスク、価格上限、数量、資金影響、警告を先に出す。
- 第2層はscenario、恒久損失8軸、重要主張、source、不足情報、反対仮説を保持する。
- analysis、independent review、human decision、historical outcome、order、executionを別 artifactにする。
- source lineageは重要値の各sectionに持たせ、任意のclaim一覧だけに依存しない。
- 即時購入、浅い指値、深い指値、見送りを比較し、価格上限と待機資金下限を破らない。
- 数値はfinite、価格は正、板はbid <= ask、scenarioとclaim IDは一意、JSONはstrictとする。

## 削除または詳細層へ送る候補

- 判断を変えない長いマクロ叙述
- 候補、thesis、HTML reportに重複する同一数値と文章
- 根拠のない単一総合リスクスコア
- 旧FV、2年収束expected yield、RRと5年scenarioの並列表示
- 正式契約になる前のwalking-skeleton CLIとfixture schema

旧FV 1,300 円、期待利回り 14.4%、RR 2.5 は新しい5年scenarioとの比較用に第1層へ残した。
どちらか一方で判断できると受入確認できた場合は、後続Issueで重複指標を削除する。

## ユーザー受入で確認すること

1. 第1層だけで `approve` / `defer` / `reject` の検討を開始できるか。
2. 第2層で重要数値の根拠、不足、最強の反対仮説を追えるか。
3. 第1層に不足する情報、または表示不要な情報は何か。

この手動確認が #329 の最後の受入条件である。コード上では人間判断を生成せず、入力された
判断を対象proposalへ束縛して記録するだけに留める。
