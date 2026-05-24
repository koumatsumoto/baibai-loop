# screening/mechanical.md

Baibai-Loop の **狭義のスクリーニング**（機械的ふるい）の仕様。`records/04-candidates/` の出力を決める playbook-linked screen rule。

## 1. 位置付け

- Decision lifecycle の **screen output (`records/04-candidates/`)** の中核
- universe（[`universe-rules.md`](./universe-rules.md)）× valuation / cash / CF / sales 指標（[`valuation-metrics.md`](./valuation-metrics.md)）を入力
- **通過銘柄 list を事実として出力**（解釈は入れない）
- research 選定の input となる

## 2. 入力

### 2.1 Universe

- 時価総額 100 億円以上
- 20 営業日平均売買代金 1 億円以上
- 上場 182 日以上
- 特別注意 / 整理銘柄 / 取引停止 / 上場廃止警告を除外
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

以下の playbook-linked screen のうち、**最低 1 つ** を満たす銘柄を通過とする。閾値は `records/_config/screening-rules/2026-05-01T000000+0900.yaml` を正本とする。

### 3.1 `valuation-reversion`

伝統的な valuation mean-reversion。旧来の 3 条件を 1 つの lane に束ね、`reasons[]` で内訳を残す。

- 業種中央値比 + 過去自己レンジ下位
- 過去 60 営業日の急落 + valuation 下方乖離
- セクターローテーションによる短期売り

EDINET が無い場合、EV/EBITDA は `unavailable` として判定対象から外す。EDINET があっても EV または EBITDA がゼロ以下の場合は、倍率としての割安解釈が成立しないため EV/EBITDA を `null` とし、この lane では使わない。PER / PBR など利用可能な指標で degrade して評価する。P/S は売上成長と営業赤字条件を伴う `sales-discount-growth` 専用 lane で扱い、valuation-reversion の単独指標にはしない。

### 3.2 `strict-net-cash-discount`

EDINET `type=5` CSV から抽出した cash と interest-bearing debt を使い、`net_cash = cash - debt` を機械的に算出する。J-Quants の CashEq proxy ではなく、より厳密な net cash を使うため、同じ銘柄が `cash-rich-asset-discount` と重なる場合はこの lane を primary にする。

- `net_cash_to_market_cap` が閾値以上
- `price_to_equity` が閾値以下
- `equity_ratio` が閾値以上
- 営業赤字ではない
- `ttm_quality_net_cash != unavailable`
- `edinet_failure_reasons` に `debt_assumed_zero` が含まれない

銀行・証券・保険・その他金融、電気・ガス業は除外する。金融業の負債は通常の事業会社の有利子負債と同じ意味で読めず、電気・ガス業は規制・設備投資・燃料費調整を見ないと net cash の下値余地を機械判定しにくいため。

### 3.3 `fcf-yield-discount`

EDINET `type=5` CSV から抽出した営業 CF と設備投資支出を使い、`FCF = EDINET CFO - capex` として FCF yield を算出する。OCF yield だけでは設備投資負担の大きい企業を安く見誤るため、CF 系の中ではこの lane を優先して見る。J-Quants 財務サマリー由来の `ocf_ttm` は別 source のため、FCF evidence の再計算には混ぜない。

- `fcf_yield` が閾値以上
- FCF がプラス
- CFO YoY が大きく悪化していない
- `ttm_quality_fcf_yield = exact`

銀行・証券・保険・その他金融、電気・ガス業は除外する。金融業の営業 CF は通常の事業会社の現金創出力と同じ意味で比較しにくく、電気・ガス業は規制設備産業として capex 解釈を research で個別確認する必要が大きいため。

### 3.4 `cash-rich-asset-discount`

CashEq / market cap、price-to-equity、equity ratio を使い、厳密 net cash ではないが、cash-rich / asset discount 候補を拾う。J-Quants 財務サマリーのみで完結させ、有利子負債は research で一次確認する。

銀行・証券・保険・その他金融はこの lane から除外する。金融業の balance sheet は通常の事業会社と意味が異なり、CashEq / market cap を margin of safety として機械判定しにくいため。

電気・ガス業もこの lane から除外する。規制・設備産業では CashEq / market cap が高くても、有利子負債・設備投資・燃料費調整などを見ないと margin of safety として読みにくいため。

この lane は EDINET metrics が欠ける銘柄の proxy / downgrade として残す。ただし EDINET の `net_cash_to_market_cap` が取得でき、設定値を下回る場合は、CashEq proxy が高くても cash-rich evidence hit を出さない。J-Quants の CashEq だけで「現金が厚い」と見えても、EDINET の有利子負債を差し引くと net debt である銘柄を research 優先候補に上げないためである。EDINET で `strict-net-cash-discount` が成立する銘柄では、research の primary thesis は原則 `strict-net-cash-discount` に寄せる。

### 3.5 `cashflow-yield-discount`

期間正規化した CFO TTM から OCF yield を算出し、営業 CF がプラスで、CF 悪化が大きくない銘柄を拾う。TTM が作れない銘柄はこの lane から除外する。

この lane は `ocf_yield` だけでは通過させない。comparable period の `cfo_yoy` を確認し、設定された下限を下回る銘柄、または `cfo_yoy_required: true` で `cfo_yoy` が作れない銘柄は除外する。

銀行・証券・保険・その他金融、電気・ガス業はこの lane から除外する。金融業の営業 CF は通常の事業会社の現金創出力と同じ意味で比較しにくく、電気・ガス業は設備投資前の OCF yield だけでは割安性を機械判定しにくいため。

この lane は EDINET FCF が作れない銘柄の proxy としても使う。EDINET で `fcf-yield-discount` が成立する銘柄では、research の primary thesis は原則 `fcf-yield-discount` に寄せる。

### 3.6 `sales-discount-growth`

P/S が業種中央値比で安く、売上成長が残る銘柄を拾う。営業赤字銘柄は CFO プラスまたは営業赤字縮小が確認できる場合に限り許容する。

銀行・証券・保険・その他金融はこの lane から除外する。金融業の P/S は通常の事業会社の売上倍率とは意味が異なるため。

### 3.7 OR 条件の意味

- **最低 1 つ満たせば通過**
- 複数 screen hit が重なる銘柄は research 優先度を上げる
- candidates YAML の `evidence_hits[]` に lane 名、playbook、hit reasons、判定に使った metrics を記録する

## 3.8 Selection lens との境界

`fast-dislocation`、`long-hold survivability`、`shareholder return` は `select` 側の lens であり、現時点では mechanical screen の新しい hard gate ではない。

- `fast-dislocation` は急落銘柄を拾うが、急落だけでは通さず、OCF / FCF / net cash / equity buffer / sales+profit の fundamental guard を原則 2 件以上、かつ cash-flow / balance-sheet / profitability の guard family を原則 2 系統以上要求する。出来高 spike と 52 週安値距離は補助情報であり、価格下落なしでは fast-dislocation eligible にしない
- `long-hold survivability` は `high|medium|low|unknown` の annotation。短期 thesis が外れたときの保有耐性を早く見るための補助で、採用可否を単独では決めない
- `shareholder return` は配当利回り戦略ではなく、保有期間が伸びた場合の fallback evidence。自動データがない場合は `unknown`
この境界により、`records/04-candidates/` は引き続き事実層として維持し、短期の値動きや過去 research decision を使った research queue 調整は `select` output に閉じる。

## 4. 出力

### 4.1 Path

```
records/04-candidates/YYYY/MM/YYYY-MM-DD.yaml
```

1 実行 = 1 ファイル（週次運用）

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

JPX 規制情報は universe 定義の一部であり、必須 source が欠ける場合は candidates YAML を生成しない。EDINET 前処理済み metrics は任意 source であり、未ロード時は EV/EBITDA などを `unavailable` として degrade する。

## 7. Retro での調整

月次 retro で以下を評価:

- **playbook-linked screen 別 hit 数**: 多すぎる / 少なすぎる場合は `screening-rules/<effective_from>.yaml` の閾値調整候補
- **採用率**: 通過銘柄のうち research で採用された割合
- **missed opportunity tracking**: 見送り / 保留 / 未実行候補の事後パフォーマンス
- **lane 別の成功 / 失敗分類**: どの割安タイプが機能したか

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
