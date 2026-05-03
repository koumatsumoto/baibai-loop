# components/outlook.md

Baibai-Loop 4 成分アーキテクチャの **(c) マクロ見解** の運用仕様。brief を積み上げて作成されるマクロ戦略の簡易版で、`records/04-research/` の Macro gate 判定の唯一の source となる。全体構造は [`../architecture-v1.md`](../architecture-v1.md) を参照。

## 1. 役割

- `records/01-brief/` の積み上げを source として、**業種/地域/資産クラス別の追い風 (tailwind) / 中立 (neutral) / 逆風 (headwind) 評価** を生成
- `records/04-research/` の Macro gate 判定で参照される（v1 では唯一の gate source）
- Macro track の出力として、Micro track の research 選定に影響する

## 2. Bootstrap 規則（v1 運用 Day 1）

`records/04-research/` を作る前に、以下の手順で最新の outlook を用意する。

### 2.1 Day 1 必須タスク

1. まず、当日時点で利用可能な `records/01-brief/` を読む。最低限として既存 brief 群を読むが、**最新 brief が 5 営業日以上古い場合は `world-daily` または `event` を先に追加して freshness gap を埋める**
2. `records/02-outlook/YYYY/MM/outlook-YYYY-MM-DD-<slug>.yaml` を作成する（日付は実際の観測日を使う）
3. `updated_from` には、**実際に判定根拠として使った brief YAML を列挙**する。最低限の履歴だけでなく、直近の weekly / daily / event を含めてよい
4. `sectors` は東証 33 業種を全件必須、`regions` は 4 地域を全件必須で埋める。判定材料が不足する場合は `status: null` + `rationale` で明示する（省略は不可）
5. `horizon: "1-6m"` で 1-6 か月先の見通しを記述
6. bootstrap outlook 作成後、通常の research 作成フローに遷移できる

### 2.2 Bootstrap outlook の YAML 例

```yaml
schema_version: 1
ai_draft: true
published_at: "2026-04-27T09:00:00+09:00"
horizon: "1-6m"
updated_from:
  - records/01-brief/2026/01/2026-01-macro-monthly-overview.yaml
  - records/01-brief/2026/02/2026-02-macro-monthly-jp-core-cpi-sub2.yaml
  - records/01-brief/2026/03/2026-03-macro-monthly-us-cpi-3p3.yaml
  - records/01-brief/2026/04/2026-04-10-world-weekly-us-10y-down.yaml
  - records/01-brief/2026/04/2026-04-19-world-weekly-us-iran-deescalation.yaml
  - records/01-brief/2026/04/2026-04-24-world-daily-jp-cpi-mar-us-retail.yaml
summary: "..."
sectors:
  "情報・通信業":
    status: neutral
    rationale: "マクロから直接の sector signal が弱く、個別要因優位とみなす"
    source_refs:
      - records/01-brief/2026/03/2026-03-macro-monthly-us-cpi-3p3.yaml
  # ... 東証 33 業種を全件記入する
regions:
  us:
    status: neutral
    rationale: "CPI 再加速は逆風だが、小売売上高は強い。相殺して neutral"
    source_refs: [records/01-brief/2026/03/2026-03-macro-monthly-us-cpi-3p3.yaml]
  japan-domestic: { status: neutral, rationale: "...", source_refs: [...] }
  japan-external-demand: { status: tailwind, rationale: "...", source_refs: [...] }
  emerging: { status: null, rationale: "材料不足", source_refs: [] }
changes: []
next_triggers:
  - date: "2026-04-28 〜 29"
    text: "FOMC 会合"
```

bootstrap の段階では保守的に neutral を多くすることを推奨する（headwind 判定は research 採用不可を招くため、情報不足では保守的に）。

### 2.3 Bootstrap 完了後

通常の更新 trigger（3.1）に従って outlook を更新する。bootstrap outlook は第 1 版であり、第 2 版以降は追加 brief を反映して更新する。

### 2.4 null status の扱い

`status: null` とした sector / region は、research 側での Macro gate 判定では **`neutral` 扱い** とする。保守的側（`headwind` 扱い）にはしない（情報不足で過度に厳格化すると採用率が極端に下がるため）。この読み替えは `records/04-research/` 作成時の macro gate 算出で実施し、[`../screening/macro-gate-procedure.md`](../screening/macro-gate-procedure.md) を正とする。`null` でも `rationale` は必須。outlook が充実してきたら `null` を `tailwind` / `neutral` / `headwind` の明示的な判定に更新する。

## 3. 更新 trigger と頻度

### 3.1 定期

- **月次 1 回**（月初 3 営業日以内）
- 直近 1 か月の brief（週次 + 月次を基本、必要に応じて daily / event を追加）を合成して更新
- 標準の `published_at` は、必要な `world-daily` / `event` を取り込んだ **翌営業日朝（JST 06:00-10:00）** とする。同日中に出すのは緊急更新時のみ

### 3.2 不定期

以下の場合は即座に更新:

- BOJ 金融政策決定会合で決定内容があった場合（利上げ・利下げ・YCC 調整等）
- FOMC 会合で決定内容があった場合
- CPI 大振れ（予想対比 ±0.5% 以上乖離）
- 主要指数 ±5% 以上変動（Nikkei 225 / S&P 500）
- 重大地政学 shock 後

## 4. Path と命名

```
records/02-outlook/YYYY/MM/outlook-YYYY-MM-DD-<slug>.yaml
```

- `<slug>`: 内容を示す英小文字ハイフン区切り（例: `bootstrap`, `q2-outlook`, `post-boj-april`, `cpi-3p3-reaction`）

## 5. YAML 必須項目

```yaml
schema_version: 1
ai_draft: true | false
published_at: "ISO 8601"
horizon: "1-6m"                     # 想定先読み期間
updated_from:                       # この outlook を作る元になった brief YAML
  - records/01-brief/YYYY/MM/...yaml
summary: <1 段落の要約>
sectors:                            # 東証 33 業種を全件必須
  "水産・農林業":
    status: tailwind | neutral | headwind | null
    rationale: <判定根拠>
    source_refs: [records/01-brief/.../*.yaml]
  # ... 33 業種を全件記入する
regions:                            # 4 地域を全件必須
  us:                    { status: ..., rationale: ..., source_refs: [...] }
  japan-domestic:        { status: ..., rationale: ..., source_refs: [...] }
  japan-external-demand: { status: ..., rationale: ..., source_refs: [...] }
  emerging:              { status: ..., rationale: ..., source_refs: [...] }
changes:                            # 前回 outlook からの判定変更
  - target: <sector or region>
    from_status: ...
    to_status: ...
    rationale: ...
    source_refs: [...]
next_triggers:
  - date: "YYYY-MM-DD"
    text: "<次回更新 trigger となるイベント>"
```

- `schema_version`: 現状は `1`
- `ai_draft`: AI 下書き段階では `true`、人間確認後 `false`
- `sectors` のキーは東証 33 業種の正式名称を全件使う（[`../screening/valuation-metrics.md`](../screening/valuation-metrics.md) 参照）。省略は不可
- `regions` は `us` / `japan-domestic` / `japan-external-demand` / `emerging` の 4 件を全件必須
- 判定できない項目は `status: null` + `rationale` で明示する（key の省略は不可）
- 詳細な schema は [`../../records/_schemas/outlook-v1.json`](../../records/_schemas/outlook-v1.json)

### 5.1 `updated_from` の選び方

- `updated_from` は「存在する brief の全列挙」ではなく、**今回の判定に効いた canonical input 集** を書く
- 通常更新では、**前回 outlook 以降に追加された brief すべて + 前回 outlook の tailwind/headwind 判定を支えた brief の最新版** を入れる
- bootstrap では、初回判定に実際に使った brief を列挙する。freshness gap を埋めるために追加した `world-daily` / `event` も含めてよい
- これにより、outlook の鮮度と追跡可能性を両立する

### 5.2 sector / region の責務分離

- 同一マクロ根拠を `sectors` と `regions` の両方に重ねて tailwind/headwind 化しない
- 円安、外需、米最終需要のような **横断的要因** は `regions.japan-external-demand` などの地域軸へ寄せる
- `sectors` に tailwind/headwind を付けるのは、その業種固有の追加根拠がある場合に限る
- これにより、research 側の macro gate で同一要因を二重計上しない

### 5.3 schema 検証

outlook YAML の構造、必須キー、`sectors` の 33 業種完全性、`regions` の 4 地域完全性、`status` 許容値（`tailwind` / `neutral` / `headwind` / `null` 以外は error）、`updated_from` / `source_refs` の `.yaml` 末尾は `baibai-loop-validate` で検査される。CI の `Validate artefacts` step で merge gate になる。手元では `uv run baibai-loop-validate --target outlook` で個別に走らせられる。

## 6. フィールドの書き方

- outlook は **分析層**（philosophy 柱 1）。解釈を書いてよい
- ただし、根拠となる brief への参照を必ず付ける（`source_refs` / `updated_from`）
- `summary` は 1 段落で現在のマクロ見解を要約
- 各 sector / region の `rationale` は判定根拠を 1〜2 文で記述する
- `changes` には前回 outlook からの判定変更を `target` / `from_status` / `to_status` / `rationale` で構造化する
- `next_triggers` は次に outlook を更新すべきイベントを列挙
- 投資判断の示唆は軽く（「このマクロ下では... が相対的に有利」程度）、個別銘柄への言及はしない（それは research の仕事）

## 7. research への接続

### 7.1 Macro gate 判定

`records/04-research/` の front matter `macro_gate` は、この outlook の `sectors` / `regions` を参照して決まる:

- 対象銘柄の属する業種・地域の outlook 判定を取得
- 業種と地域で判定が食い違う場合は **保守的な方を採用**（headwind >> neutral >> tailwind の優先順位）
- 詳細: [`../screening/macro-gate-procedure.md`](../screening/macro-gate-procedure.md)

### 7.2 outlook 未更新時

- 最新の outlook が古く、その後出た緊急 brief で状況が変わった場合:
  - 該当 brief を research の `brief_refs` に追加
  - gate 判定を **保守側にのみ** 手動上書き可（tailwind → neutral、neutral → headwind。逆向きの上書き不可）
- 常態的に outlook が遅れるようなら、outlook の更新 trigger を見直す

## 8. Future work: analysis 集約層への置換

- 将来、`records/01-brief/` の上位に `analysis/` 集約層（産業別 AI 分析集約）を導入する構想がある（philosophy §6「v1 の時点で意図的に残す未熟さ」の未熟さ 1）
- その時は `records/04-research/` の `outlook_ref` を `analysis_ref` に切り替える
- v1 では outlook 手動運用のまま。置換可能な設計を意識して outlook schema を stable に保つ

## 9. AI の役割境界

| 作業 | AI 可 | 人間のみ |
| --- | --- | --- |
| brief の読み込み・要点抽出 | ○ | |
| outlook 下書き生成 | ○ | |
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
- [`../templates/outlook.yaml`](../templates/outlook.yaml): template
