# World Analysis: YYYY-MM-DD {kind} ({slug})

対象期間: YYYY-MM-DD 〜 YYYY-MM-DD
観測日: YYYY-MM-DD
市場データの基準日: YYYY-MM-DD 終値（直近営業日）

階層: **世界情勢 → 日本経済 → 日本株** の順に記録する。詳細は [../design-principles.md](../design-principles.md) 参照。

月次統計（CPI / 雇用統計 / 政策金利変更 等）はこのテンプレートでは記録せず、該当月の `macro-monthly` journal を参照する。

直近の月次 journal: `journal/YYYY/MM/YYYY-MM-macro-monthly-{slug}.md`（未作成の場合はその旨を明記）

## 1. 世界情勢

### 1.1 グローバルマーケット指標

| 指標 | 値 | 週次コメント | ソース |
|---|---|---|---|
| 米 10Y 利回り | X.XX% | +X.X bp (週次) | [FRED DGS10](URL) (YYYY-MM-DD取得) |
| 米 2Y 利回り | X.XX% | +X.X bp (週次) | [FRED DGS2](URL) (YYYY-MM-DD取得) |
| 10Y-2Y スプレッド | +XX bp | — | 上記 2 値より算出 |
| VIX | XX.X | +X.X% (週次) | [FRED VIXCLS](URL) (YYYY-MM-DD取得) |
| Brent 原油 | $XX.XX | +X.X% (週次) | [FRED DCOILBRENTEU](URL) (YYYY-MM-DD取得) |
| WTI 原油 | $XX.XX | +X.X% (週次) | [FRED DCOILWTICO](URL) (YYYY-MM-DD取得) |
| 銅 (LME, 3 か月物) | $X,XXX | — | [FRED](URL) (YYYY-MM-DD取得) |
| 金 (LBMA) | $X,XXX | — | [FRED](URL) (YYYY-MM-DD取得) |
| FedWatch (次回会合 据え置き/利上げ/利下げ期待) | XX% | — | [CME FedWatch](URL) (YYYY-MM-DD取得) |

### 1.2 地政学・グローバルイベント

- YYYY-MM-DD: [事実記述のみ。禁止表現（観測・背景として・受けて・示唆・思われる）を使わない] [ソース名](URL) (YYYY-MM-DD取得)

## 2. 日本経済

### 2.1 為替（クロス円中心）

| 指標 | 値 | 週次コメント | ソース |
|---|---|---|---|
| USD/JPY | XXX.XX | +X.X% (週次) | [FRED DEXJPUS](URL) (YYYY-MM-DD取得) |
| EUR/JPY | XXX.XX | +X.X% (週次) | [FRED](URL) (YYYY-MM-DD取得) |
| AUD/JPY | XXX.XX | +X.X% (週次) | [FRED](URL) (YYYY-MM-DD取得) |

### 2.2 日本のイベント・速報

- YYYY-MM-DD: [日銀総裁発言、貿易統計、景気動向指数などで週次に拾う価値があるもの] [ソース名](URL) (YYYY-MM-DD取得)

## 3. 日本株

### 3.1 マーケット指標

| 指標 | 値 | 週次コメント | ソース |
|---|---|---|---|
| 日経平均 | XX,XXX | +X.X% (週次) | [FRED NIKKEI225](URL) (YYYY-MM-DD取得) |
| TOPIX | X,XXX | +X.X% (週次) | [JPX](URL) (YYYY-MM-DD取得) |
| 東証プライム売買代金 (日次平均) | X.X 兆円 | — | [JPX](URL) (YYYY-MM-DD取得) |

### 3.2 業種別騰落（週次、上位・下位各 3）

| 区分 | 業種 | 週次騰落率 | ソース |
|---|---|---|---|
| 上位 | [業種1] | +X.X% | [JPX](URL) (YYYY-MM-DD取得) |
| 上位 | [業種2] | +X.X% | [JPX](URL) (YYYY-MM-DD取得) |
| 上位 | [業種3] | +X.X% | [JPX](URL) (YYYY-MM-DD取得) |
| 下位 | [業種4] | -X.X% | [JPX](URL) (YYYY-MM-DD取得) |
| 下位 | [業種5] | -X.X% | [JPX](URL) (YYYY-MM-DD取得) |
| 下位 | [業種6] | -X.X% | [JPX](URL) (YYYY-MM-DD取得) |

### 3.3 日本株イベントカレンダー

- YYYY-MM-DD 〜 YYYY-MM-DD: [決算発表集中日・配当権利落ち・主要銘柄イベント等] [ソース名](URL) (YYYY-MM-DD取得)

## 4. 次回主要イベント予定

- YYYY-MM-DD 〜 YYYY-MM-DD: [FOMC / 日銀 / 主要指標発表] [ソース名](URL) (YYYY-MM-DD取得)

---

記入ルールは [../workflow.md](../workflow.md) を、設計根拠は [../design-principles.md](../design-principles.md) を参照。
