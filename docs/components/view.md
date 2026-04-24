# components/view.md

Baibai-Loop 4 成分アーキテクチャの **(c) マクロ見解** の運用仕様。brief を積み上げて作成されるマクロ戦略の簡易版で、`research/` の Macro gate 判定の唯一の source となる。全体構造は [`../architecture-v1.md`](../architecture-v1.md) を参照。

## 1. 役割

- `brief/` の積み上げを source として、**業種/地域/資産クラス別の追い風 (tailwind) / 中立 (neutral) / 逆風 (headwind) 評価** を生成
- `research/` の Macro gate 判定で参照される（v1 では唯一の gate source）
- Macro track の出力として、Micro track の research 選定に影響する

## 2. Bootstrap 規則（v1 運用 Day 1）

**重要: v1 運用開始時点で `view/` は存在しない**。この状態で `research/` を作るためには、以下の bootstrap 手順を必須とする。

### 2.1 Day 1 必須タスク

1. 既存 `brief/` 全ファイル（migration 後の 5 件）を読む
2. `view/2026/04/view-YYYY-MM-DD-bootstrap.md` を手動で作成する（日付は実際の Day 1 の日付を使う）
3. front matter の `updated_from` に使った brief ファイル 5 件を全て列挙
4. `sectors` / `regions` は既存 brief から読み取れる範囲で記入。読み取れない業種/地域は `null` 許容（保守的に neutral とする選択肢も可）
5. `horizon: "1-6m"` で 1-6 か月先の見通しを記述
6. bootstrap view 作成後、通常の research 作成フローに遷移できる

### 2.2 Bootstrap view の front matter 例

```yaml
---
published_at: "2026-04-25T09:00:00+09:00"
horizon: "1-6m"
updated_from:
  - brief/2026/01/2026-01-macro-monthly-overview.md
  - brief/2026/02/2026-02-macro-monthly-jp-core-cpi-sub2.md
  - brief/2026/03/2026-03-macro-monthly-us-cpi-3p3.md
  - brief/2026/04/2026-04-10-world-weekly-us-10y-down.md
  - brief/2026/04/2026-04-19-world-weekly-us-iran-deescalation.md
sectors:
  "情報・通信": neutral
  "銀行": neutral
  "不動産": neutral
  # 記入できない業種は省略 or null
regions:
  us: neutral
  japan-domestic: neutral
  emerging: null
---
```

bootstrap の段階では保守的に neutral を多くすることを推奨する（headwind 判定は research 採用不可を招くため、情報不足では保守的に）。

### 2.3 Bootstrap 完了後

通常の更新 trigger（3.1）に従って view を更新する。bootstrap view は第 1 版であり、第 2 版以降は追加 brief を反映して更新する。

## 3. 更新 trigger と頻度

### 3.1 定期

- **月次 1 回**（月初 3 営業日以内）
- 直近 1 か月の brief（週次 4 件 + 月次 1 件）を合成して更新

### 3.2 不定期

以下の場合は即座に更新:

- BOJ 金融政策決定会合で決定内容があった場合（利上げ・利下げ・YCC 調整等）
- FOMC 会合で決定内容があった場合
- CPI 大振れ（予想対比 ±0.5% 以上乖離）
- 主要指数 ±5% 以上変動（Nikkei 225 / S&P 500）
- 重大地政学 shock 後

## 4. Path と命名

```
view/YYYY/MM/view-YYYY-MM-DD-<slug>.md
```

- `<slug>`: 内容を示す英小文字ハイフン区切り（例: `bootstrap`, `q2-outlook`, `post-boj-april`, `cpi-3p3-reaction`）

## 5. Front matter 必須項目

```yaml
---
published_at: "ISO 8601"
horizon: "1-6m"                     # 想定先読み期間
updated_from:                       # この view を作る元になった brief
  - brief/YYYY/MM/world-weekly-*.md
  - brief/YYYY/MM/*-macro-monthly-*.md
sectors:                            # 業種別 gate 判定（東証 33 業種ベース）
  "情報・通信": tailwind
  "銀行": neutral
  "不動産": headwind
regions:                            # 地域別 gate 判定
  us: tailwind
  japan-domestic: neutral
  emerging: headwind
---
```

- `sectors` のキーは東証 33 業種の正式名称を使う（[`../screening/valuation-metrics.md`](../screening/valuation-metrics.md) 参照）
- `regions` は `us` / `japan-domestic` / `japan-external-demand` / `emerging` / その他国コード等
- 判定できない項目は省略 or `null`（空欄を許容）

## 6. 本文の構成

### 6.1 推奨節構成

- **Executive summary**: 1 段落で現在のマクロ見解を要約
- **主要 brief の要点集約**: `updated_from` に挙げた各 brief のどこが effective だったか
- **業種別判定の根拠**: なぜその業種を tailwind/neutral/headwind と判定したか
- **地域別判定の根拠**: 同上
- **変化ポイント**: 前回 view からの判定変更と理由
- **次回更新 trigger の想定**: 次に view を更新すべきイベント

### 6.2 書き方

- view は **分析層**（philosophy 柱 1）。解釈を書いてよい
- ただし、根拠となる brief への参照を必ず付ける（`updated_from` の brief への link）
- 投資判断の示唆は軽く（「このマクロ下では... が相対的に有利」程度）、個別銘柄への言及はしない（それは research の仕事）

## 7. research への接続

### 7.1 Macro gate 判定

`research/` の front matter `macro_gate` は、この view の `sectors` / `regions` を参照して決まる:

- 対象銘柄の属する業種・地域の view 判定を取得
- 業種と地域で判定が食い違う場合は **保守的な方を採用**（headwind >> neutral >> tailwind の優先順位）
- 詳細: [`../screening/macro-gate-procedure.md`](../screening/macro-gate-procedure.md)

### 7.2 view 未更新時

- 最新の view が古く、その後出た緊急 brief で状況が変わった場合:
  - 該当 brief を research の `brief_refs` に追加
  - gate 判定を **保守側にのみ** 手動上書き可（tailwind → neutral、neutral → headwind。逆向きの上書き不可）
- 常態的に view が遅れるようなら、view の更新 trigger を見直す

## 8. Future work: analysis 集約層への置換

- 将来、`brief/` の上位に `analysis/` 集約層（産業別 AI 分析集約）を導入する構想がある（philosophy §6「v1 の時点で意図的に残す未熟さ」の未熟さ 1）
- その時は `research/` の `view_ref` を `analysis_ref` に切り替える
- v1 では view 手動運用のまま。置換可能な設計を意識して view schema を stable に保つ

## 9. AI の役割境界

| 作業 | AI 可 | 人間のみ |
| --- | --- | --- |
| brief の読み込み・要点抽出 | ○ | |
| view 下書き生成 | ○ | |
| sectors / regions 判定の初期案 | ○ | 最終確定は人間 |
| 反対論点の列挙 | ○ | |
| **最終判定（tailwind/neutral/headwind）の確定** | | ○ |
| **判定根拠の最終確認** | | ○ |

## 10. 参考

- [`../philosophy.md`](../philosophy.md): 思想（マクロ優位 76/24）
- [`../architecture-v1.md`](../architecture-v1.md): 全体構造、Bootstrap 規則
- [`brief.md`](./brief.md): source となる brief の仕様
- [`research.md`](./research.md): 接続先 research の仕様
- [`../screening/macro-gate-procedure.md`](../screening/macro-gate-procedure.md): Macro gate 判定手順
- [`../templates/view.md`](../templates/view.md): template
