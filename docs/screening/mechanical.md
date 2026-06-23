# screening/mechanical.md

Baibai-Loop の **狭義のスクリーニング**（機械的ふるい）の仕様。`records/04-candidates/` の出力を決める playbook-linked screen rule。

## 1. 位置付け

- Decision lifecycle の **screen output (`records/04-candidates/`)** の中核
- universe（[`universe-rules.md`](./universe-rules.md)）× valuation / cash / CF / sales 指標（[`valuation-metrics.md`](./valuation-metrics.md)）を入力
- **通過銘柄 list を事実として出力**（解釈は入れない）
- research 選定の input となる

## 2. 入力

### 2.1 Scope

- 全上場普通株（プライム / スタンダード / グロース、直近 20 営業日以上の bar 履歴）
- 時価総額・売買代金・上場期間・JPX 規制 flag は除外条件ではなく candidates に記録される事実。research 推奨への絞り込みは `selection.liquidity` が分析層で適用する
- sector / 市場中央値の比較母集団は `selection.liquidity` を満たす流動性母集団に固定する
- 詳細: [`universe-rules.md`](./universe-rules.md)

### 2.2 指標

- PER（forward 優先、会社予想ベース、未公表時は trailing のみ）
- PBR
- EV/EBITDA（EDINET 前処理済み metrics がある場合のみ）
- P/S
- PCFR
- OCF yield（CFO TTM / market cap）
- FCF yield（FCF TTM / market cap、EDINET CSV-derived metrics がある場合のみ）
- cash-to-market-cap / price-to-equity
- net-cash-to-market-cap / price-to-equity（EDINET CSV-derived metrics がある場合のみ）
- price_change_1d / 5d / 20d / 60d、gap_from_52w_low、turnover_spike_5d（select の fast-dislocation lens 用。mechanical screen の hard gate にはしない）
- 業種中央値（東証 33 業種、n<10 は市場全体 fallback）
- 過去 3 年自己レンジ（上場 3 年未満は上場来）
- 詳細: [`valuation-metrics.md`](./valuation-metrics.md)

## 3. Playbook-linked screen（OR 条件、最低 1 つ満たす）

以下の playbook-linked screen のうち、**最低 1 つ** を満たす銘柄を通過とする。閾値は `records/_config/screening-rules/2026-06-19T000000+0900.yaml` を正本とする。

### 3.1 `valuation-reversion`

伝統的な valuation mean-reversion。旧来の 3 条件を 1 つの playbook に束ね、`reasons[]` で内訳を残す。

- 業種中央値比 + 過去自己レンジ下位
- 過去 60 営業日の急落 + valuation 下方乖離
- セクターローテーションによる短期売り

PER は `per_forward`(会社予想ベース)を primary とし、forecast EPS が未取得の銘柄は `per_trailing` に degrade する。EDINET が無い場合、EV/EBITDA は `unavailable` として判定対象から外す。EDINET があっても EV または EBITDA がゼロ以下の場合は、倍率としての割安解釈が成立しないため EV/EBITDA を `null` とし、この playbook では使わない。PER / PBR など利用可能な指標で degrade して評価する。P/S は売上成長と営業赤字条件を伴う `sales-discount-growth` 専用 playbook で扱い、valuation-reversion の単独指標にはしない。

銀行・証券・保険・その他金融はこの playbook から除外する。金融業の PER / PBR は規制資本・金利環境・与信サイクルの構造要因を含み、事業会社と同じ mean-reversion の前提で機械判定できないため。他 playbook と異なり電気・ガス業は除外しない。BS / CF の機械判定が成立しないという他 playbook の除外根拠は、相対 valuation の比較には当たらないため。

### 3.2 `cash-rich-asset-discount`

CashEq / market cap、price-to-equity、equity ratio を使い、cash-rich / asset discount 候補を拾う。J-Quants 財務サマリーと EDINET 双方を input にする。

- `cash_to_market_cap` が閾値以上
- `price_to_equity` が閾値以下
- `equity_ratio` が閾値以上
- 営業赤字ではない
- `operating_profit_yoy` が `operating_profit_yoy_deterioration_threshold` (-0.3) を下回る銘柄は除外。BS が rich でも営業エンジンが急減速している銘柄を排除する deterioration gate
- EDINET の `net_cash_to_market_cap` が取得できる場合、設定下限を下回る銘柄は除外（J-Quants CashEq が高くても有利子負債を差し引くと net debt な銘柄を排除）

銀行・証券・保険・その他金融、電気・ガス業、卸売業、不動産業は除外する。金融業の BS は意味が異なり、規制設備産業の CashEq / market cap は機械判定として読みにくく、卸売業 (商社・問屋) は運転資金で J-Quants `cash_eq` が機械判定上膨張、不動産業は land inventory が `equity_ratio` / `cash_to_market_cap` を歪めるため。

`docs/operations/backtest-runbook.md` §6 の 2026-05 playbook-cohorts では 4w mean rel −2.14pt (baseline 比 +5.32pt) で全 playbook 中最強。本 playbook を維持する根拠データ。

### 3.3 `cashflow-yield-discount`

期間正規化した CFO TTM から OCF yield を算出し、営業 CF がプラスで、CF 悪化が大きくない銘柄を拾う。TTM が作れない銘柄はこの playbook から除外する。

この playbook は `ocf_yield` だけでは通過させない。comparable period の `cfo_yoy` を確認し、設定された下限を下回る銘柄、または `cfo_yoy_required: true` で `cfo_yoy` が作れない銘柄は除外する。

`operating_profit_yoy` が `operating_profit_yoy_deterioration_threshold` (-0.3) を下回る銘柄も除外。OCF は高いのに営業利益 YoY が急減している銘柄を排除する deterioration gate。

`fcf_yield_required_positive: true` で `fcf_yield` が判定可能 (EDINET 取得済) かつ <= 0 の銘柄は除外。OCF プラスだが capex 先行で FCF マイナス (重設備) は短期 mean-reversion で機械判定しにくく cohort 成績悪化要因のため。EDINET 不在で `fcf_yield is None` の銘柄は data 不在として通過させる。

銀行・証券・保険・その他金融、電気・ガス業はこの playbook から除外する。金融業の営業 CF は通常の事業会社の現金創出力と同じ意味で比較しにくく、電気・ガス業は設備投資前の OCF yield だけでは割安性を機械判定しにくいため。

### 3.4 `sales-discount-growth`

P/S が業種中央値比で安く、売上成長が残る銘柄を拾う。営業赤字銘柄は CFO プラスまたは営業赤字縮小が確認できる場合に限り許容する。

ただし `operating_margin_min` (-0.05) を下回る operating margin (`operating_profit / sales`) を持つ銘柄は除外する。これは「loss narrowing」(-100B → -50B でも条件 pass) の escape hatch を defang する floor で、chronic loser を排除する。`sales > 0` の銘柄でのみ enforce する。

銀行・証券・保険・その他金融はこの playbook から除外する。金融業の P/S は通常の事業会社の売上倍率とは意味が異なるため。

### 3.5 OR 条件の意味

- **最低 1 つ満たせば通過**
- 複数 screen hit が重なる銘柄は research 優先度を上げる
- candidates YAML の `evidence_hits[]` に playbook 名、hit reasons、判定に使った metrics を記録する

## 3.6 Selection lens との境界

`fast_dislocation` と `long_hold_survivability` は `select` 側の lens であり、mechanical screen の hard gate ではない。

- `fast_dislocation` は急落銘柄を拾うが、急落だけでは通さず、OCF / FCF / net cash / equity buffer / sales+profit の fundamental guard を原則 2 件以上、かつ cash-flow / balance-sheet / profitability の guard family を原則 2 系統以上要求する。出来高 spike と 52 週安値距離は補助情報であり、価格下落なしでは eligible にしない
- fast_dislocation には stabilization annotation が付く(直近 1 営業日リターンが 0 以上 = 下げ止まりの最小限の反証。固定閾値)。fast boost が有効な局面では、stabilized な急落銘柄を未だ下落中の銘柄より上位に置く(boost が regime lens で中立化されている間は不発)。計測経路は selection-ablation の `no_stabilization` variant
- `long_hold_survivability` は `high|medium|low|unknown` の annotation。短期 thesis が外れたときの保有耐性を早く見るための補助で、採用可否を単独では決めず、ranking にも使わない(ranking 寄与の計測手順は [`../operations/backtest-runbook.md`](../operations/backtest-runbook.md) §3-C)
- playbook の優先順位は config の `output.research_selection_playbook_order` を唯一の正本とし、推奨 queue の順位付けと primary evidence の選択の両方に使う。順序値の変更は config 編集 + replay / ablation 計測で検証する

この境界により、`records/04-candidates/` は事実層として維持し、短期の値動きや過去 research decision を使った調整は `select` output の `recommendations` / `selection.diagnostics` / `reason_tags` / `risk_tags` に閉じる。

### 3.6.1 Market regime lens


`select` は、`data/screening/market.sqlite` の daily bars だけから機械的に market regime snapshot を計算し、ranking lens として使う（`--no-regime-lens` で無効化、SQLite が無ければ自動で無効）。

- 算出: benchmark proxy（`1321`）の 20/60 営業日リターン (trend のみ。universe breadth は別 telemetry として `market-snapshot` の weekly history が担当する)
- 分類（固定閾値、grid search しない）: `risk_on_rally` = benchmark 20 営業日リターン >= +3% / `risk_off_selloff` = <= -3% / その他 `neutral_range`、算出不能は `unknown`
- 効果: `risk_on_rally` のときだけ fast_dislocation eligible の ranking boost を中立化する。候補の除外はしない（lens であり gate ではない）。`neutral_range` / `risk_off_selloff` / `unknown` では従来挙動と完全一致
- 根拠: fast_dislocation は anti-momentum 銘柄（直近の大幅下落銘柄）を選ぶため、指数モメンタムが強い局面では breadth の広狭に関係なく構造的に劣後する。このため分類はトレンド単独条件とする（検証は [`../operations/backtest-runbook.md`](../operations/backtest-runbook.md) §3-A）
- snapshot は `selection.diagnostics.market_regime` に記録し、中立化時は `fast_dislocation_boost: neutralized` と warning `fast_dislocation_boost_neutralized_risk_on_rally` を出す

## 4. 出力

### 4.1 Path

```
records/04-candidates/YYYY/MM/YYYY-MM-DD.yaml
```

1 実行 = 1 ファイル（週次運用）。git に積まない local store として履歴をローカル保持する（[`../components/candidates.md`](../components/candidates.md) §2）。

### 4.2 YAML

詳細は [`../components/candidates.md`](../components/candidates.md) 節 4 を参照。核心:

```yaml
candidates:
  - ticker: "7203"
    name: "..."
    per_forward: 8.2
    pbr: 0.72
    p_s: 0.6
    pcfr: 5.1
    metrics:
      ocf_yield: 0.13
      cfo_yoy: 0.08
      cash_to_market_cap: 0.42
      net_cash_to_market_cap: 0.21
      fcf_yield: 0.08
    evidence_hits:
      - name: cashflow-yield-discount
        playbook_id: cashflow-yield-discount
        reasons: [ocf_yield_discount]
```

## 5. 実行頻度

- **週次 1 回**（初期値、retro で調整）
- 実行曜日: 毎週月曜朝 or 金曜夕方（運用で決める）

## 6. 実装方針

CLI で自動化されており、実装の正本は [`automation.md`](./automation.md) を参照。本ドキュメントは mechanical ルールの意味論に絞る。

```bash
python -m baibai_loop.screening.cli run --asof YYYY-MM-DD
```

JPX 規制情報と EDINET 前処理済み metrics は screening run の必須 input である。いずれかが欠ける場合は candidates YAML を生成せず、`bootstrap-cache` / `extract-edinet-metrics` / `verify-cache-coverage` で SQLite を補完してから再実行する。EDINET metrics が存在する銘柄内で個別 metric が欠ける場合だけ、EV/EBITDA などを `unavailable` として degrade する。

## 7. Retro での調整

月次 retro で以下を評価:

- **playbook-linked screen 別 hit 数**: 多すぎる / 少なすぎる場合は `screening-rules/<effective_from>.yaml` の閾値調整候補
- **採用率**: 通過銘柄のうち research で採用された割合
- **missed opportunity tracking**: 見送り / 保留 / 未実行候補の事後パフォーマンス
- **playbook 別の成功 / 失敗分類**: どの割安タイプが機能したか

閾値変更は playbook 改訂議論に含める。

## 8. 事実と分析の分離

- mechanical は **事実層**。閾値適用・screen hit は機械的
- 「なぜ割安か」の仮説・「採用すべきか」の判断は research 側
- candidates ファイル本文には補足情報を事実として記録し、解釈を入れない

## 9. 参考

- [`principles.md`](./principles.md): スクリーニング原則
- [`universe-rules.md`](./universe-rules.md): universe 境界条件
- [`valuation-metrics.md`](./valuation-metrics.md): 指標算出仕様
- [`../components/macro-context.md`](../components/macro-context.md): screening 前の macro context
- [`../components/candidates.md`](../components/candidates.md): candidates 運用仕様
- [`../components/research.md`](../components/research.md): research 選定プロセス
