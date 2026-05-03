# screening/universe-rules.md

Baibai-Loop スクリーニングの universe（対象銘柄集合）の境界条件。`(b) records/03-candidates/` の入力となる銘柄 pool を定義する。

## 1. 対象

- **日本株普通株**のみ
- 除外: ETF / REIT / 優先株
- 上場市場: プライム / スタンダード / グロース
- TOKYO PRO Market は対象外

## 2. 時価総額

- **200 億円以上**
- 算出方法: 直近営業日終値 × 発行済株式数
- 更新頻度: 月次で universe を再取得（J-Quants Light）

## 3. 流動性

- **20 営業日平均売買代金 3 億円以上**
- 算出: 直近 20 営業日の日次売買代金の単純平均
- 取得ソース: J-Quants Light

## 4. 除外条件

### 4.1 上場期間

- **上場 6 か月未満**: 除外（IPO 直後の値動き特殊性）
- **上場 3 年未満**: universe に含めるが、P-A の「過去 3 年自己レンジ」判定は不可 → **上場来レンジで代替 or P-B 限定採用**
- 実装注記: J-Quants v2 `/listed/info` (`get_eq_master`) に listing_date フィールドが無いため、上場日の proxy として **daily bars 履歴の最古日** を使う。cache window は asof-1200 日のため、上場 3.3 年超の銘柄は実上場日と乖離し 1200 日扱いになるが、上場 6 か月・3 年の閾値判定には影響しない。

### 4.2 規制・特別指定

- **特別注意銘柄**: 除外
- **整理銘柄**: 除外
- **日々公表信用指定**: universe からは除外しないが、research で crowding 懸念材料として必ず記録

### 4.3 取引停止・上場廃止警告

- 直近で取引停止または上場廃止警告が出た銘柄は除外

## 5. 時価総額別の Position sizing 上限

詳細は [`principles.md`](./principles.md) 節 7 を参照。核心:

| 時価総額 | Position 上限 | 備考 |
| --- | --- | --- |
| 1,000 億円以上 | 2% | 標準 |
| 500〜1,000 億円 | 1% | |
| 200〜500 億円 | 0.5% | **P-B のみ**、catalyst freshness ≦ 10 営業日 + 出来高 1.5x 以上 |

## 6. Front matter 書式ルール（universe 関連）

### 6.1 日時

- ISO 8601 完全形（秒まで）、**quote 必須**
- 例: `"2026-04-24T09:00:00+09:00"`

### 6.2 ticker

- **quote 必須**
- **4 文字の英数字文字列**として扱う（先頭 0 落ち防止、英字組入れ対応）
- 例: `"7203"`, `"0036"`, `"130A"`

### 6.3 時価総額 / 売買代金

- 単位を明示（`oku` = 億円、`million` = 百万円）
- 例: `market_cap_oku: 1500`, `avg_turnover_oku: 5.2`

### 6.4 null 許容

- 取得不能・未公表フィールドは明示的に `null`
- 省略（キー自体を書かない）は避ける（schema 検証時に欠損と区別困難）

### 6.5 配列

- 参照は path 配列: `brief_refs: [...]`, `updated_from: [...]`, `candidates_ref: ...`（単一は文字列、複数は配列）

## 7. Universe 更新の運用

### 7.1 更新頻度

- **月次 1 回**: J-Quants Light から universe を再取得（上場・廃止・時価総額変動を反映）
- **週次更新なし**: 週内の時価総額変動で universe 境界をまたぐ銘柄は多くない想定

### 7.2 更新タイミング

- 月次 retro 作成時に universe 境界を再確認
- 大きな制度変更（IPO ラッシュ、規制変更等）があった場合は随時

### 7.3 履歴

- 各 `records/03-candidates/YYYY/MM/YYYY-MM-DD.yaml` の YAML `universe_size` で実行時点の universe サイズを記録
- 履歴を遡れば universe の縮小・拡大を追跡できる

### 7.4 JPX 規制情報の取得失敗

- 特別注意 / 整理 / 取引停止 / 上場廃止警告の参照に必要な JPX 公開情報（CSV / Excel / HTML）が取得できない run は、**fail-fast** として `candidates` を生成しない
- `universe` の必須除外条件に直結するため、`unknown` 扱いで run 継続しない
- 特別注意銘柄は `JPX_SPECIAL_CAUTION_INDEX_URL` があれば、日次変動する個別銘柄信用取引残高表の `mtdailyk*.xls` を index から解決する
- JPX 規制情報は latest snapshot 取得のため、7 weekday 超のバックフィルで cache が無い場合は fail-fast し、明示的な `--allow-stale-jpx` 指定時のみ latest snapshot の取得を許容する

## 8. 参考

- [`principles.md`](./principles.md): スクリーニング原則
- [`valuation-metrics.md`](./valuation-metrics.md): 指標算出仕様
- [`mechanical-v1.md`](./mechanical-v1.md): 機械的ふるい仕様
- [`../components/candidates.md`](../components/candidates.md): candidates 運用仕様
