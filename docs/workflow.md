# 運用ワークフロー

Baibai-Loop の 4 成分 + 下流アーキテクチャにおける日々の運用ルール。設計の根拠は [`design-principles.md`](./design-principles.md)、構造は [`architecture-v1.md`](./architecture-v1.md)、思想は [`philosophy.md`](./philosophy.md) を参照。

## 0. 全体ワークフロー（4 成分 + 下流）

Baibai-Loop は 2 トラック構成で運用する:

- **Macro track (独立)**: `records/01-brief/` → `records/02-outlook/`（売買イベントと独立に更新）
- **Micro track (売買ループ)**: `records/03-candidates/` → `records/04-research/` → `records/05-trades/` → `records/06-reviews/` → retro feedback

各成分の詳細運用は [`components/`](./components/) 配下の個別 doc を参照:

- [`components/brief.md`](./components/brief.md): (a) マクロ事実ブリーフ
- [`components/candidates.md`](./components/candidates.md): (b) スクリーニング通過銘柄
- [`components/outlook.md`](./components/outlook.md): (c) マクロ見解
- [`components/research.md`](./components/research.md): (d) 個別銘柄リサーチ
- [`components/trades.md`](./components/trades.md): 執行記録
- [`components/reviews.md`](./components/reviews.md): 事後検証・retro

本ファイルの以下の節は、主に **(a) brief の運用** に関するルール（世界情勢調査の記録）を扱う。他成分の運用は上記 components/ を参照。

## 1. brief の分析階層

全ての brief は「世界情勢 → 日本経済 → 日本株」の階層で記録する。各レイヤーの役割と分離方針は [design-principles.md](./design-principles.md) を参照。

## 更新頻度と kind の分離

データは更新サイクルと一致する粒度で別ファイルに分離する:

- **週次レギュラー** (`world-weekly`): マーケット指標・直近イベント・地政学速報。毎週 1 回
- **日次レギュラー** (`world-daily`): 週次まで待つと stale になる fresh fact。営業日ベースで必要な日に作成
- **月次レギュラー** (`macro-monthly`): CPI / 雇用統計 / 政策金利変更などの月次〜四半期統計。毎月 1 回（主要発表の出揃い後）
- **イベント時臨時** (`fomc` / `boj` / `cpi` / `gdp` / `geopolitics` 等): 重要イベント発生時に都度

週次 / 日次 brief は原則として月次データを再掲せず、該当月の `macro-monthly` brief へリンクで参照するだけにする。例外として、`macro-monthly` がまだ閉じていない期間は `world-daily` が新規公表された月次級データを一時的に保持してよい。

### kind の重複回避

同じイベントを複数 kind で記録しない。優先順位:

1. 政策変更や閾値超え surprise は個別イベント kind (`fomc`, `boj`, `cpi` 等) を作り、週次 brief は該当 kind へのリンクで代替する
2. routine な単一統計公表は `macro-monthly` 未作成期間なら `world-daily` に置き、`event` は作らない
3. 月次 kind (`macro-monthly`) で拾える指標は、週次 brief の指標表に再掲しない。`macro-monthly` 未作成期間は `world-daily` に置く
4. 同じ重要度のイベントが複数 kind にまたがる場合、もっとも粒度の細かい kind で記録し、他の brief からはリンクする

## ファイル配置と命名

```
records/01-brief/YYYY/MM/YYYY-MM-DD-{kind}-{slug}.yaml
```

- `{kind}` は `world-daily` / `world-weekly` / `macro-monthly` / `fomc` / `boj` / `cpi` / `gdp` / `geopolitics` などイベント種別を示す
- `{slug}` は内容を端的に示す短い英小文字ハイフン区切り文字列
  - 事実ベース: 指標名と値を組み合わせた中立表現を選ぶ（例: `us-cpi-3p3`, `jp-core-cpi-sub2`, `us-10y-down`）
  - 解釈・評価を含む語 (`beat`, `surge`, `rally`, `crash`, `hot`, `cool`) は避ける
  - 数値を含める場合、小数点は `p` で代用する (`3.3%` → `3p3`)
  - 目立つ事実がない観測月は `overview` を用いてよい
- `{kind}` 自体にハイフンを含む場合があるので、パース時は既知の kind 一覧との貪欲一致を前提とする
- INDEX ファイルは作らない。一覧は `git ls-files records/01-brief/` または GitHub 上のツリーで確認する

## 日付の扱い

- **観測日**: ファイル名の日付 = その記録を作成・確定した日
- **市場データの基準日**: マーケット指標の参照元となる直近営業日終値の日付（週末や祝日では観測日とずれる）
- **取得日**: 引用末尾の `(YYYY-MM-DD取得)` は「そのソースに今回アクセスした日付」。既存 brief と同じ URL を再利用する場合でも、今回の brief 作成時にその URL を再確認したならその日付を記載する。再確認していないなら、URL を含めて再掲しない（古いデータの流用を避ける）

## データソースと引用

- 使うソースは [data-sources.md](./data-sources.md) に限定する
- 引用は本文中にインラインで `[ソース名](URL) (YYYY-MM-DD取得)` の形式を使う
- 数値や事実はできるだけ Tier 1 から取り、Tier 2 は一次統計で拾えない事象に限定する

## outlook 作成前の brief 充足

bootstrap outlook または通常の outlook 更新の前に、brief の鮮度を確認する:

- 最新 brief が 5 営業日以上古い場合は、まず `world-daily` または `event` を追加する
- 当月の `macro-monthly` が未作成でも、その後に outlook に効く一次統計が出ていれば `world-daily` に載せてから outlook を作る
- `updated_from` は「存在する全 brief」ではなく、今回の outlook 判定に効いた brief を列挙する

## brief 作成前の欠損確認

`world-weekly` / `world-daily` / `macro-monthly` を新規作成する前に、既存 brief の時系列欠損を必ず確認する。これは Codex / Claude Code を含む AI agent の作業前チェックとして扱い、省略しない。

### 必須チェック

1. `find records/01-brief/YYYY -type f -name '*.yaml' | sort` で対象年の brief 一覧を確認する
2. `find records/01-brief/YYYY -type f -name '*world-weekly*.yaml' | sort` で週次 brief の連続性を確認する
3. 各 `world-weekly` YAML の `period.start` / `period.end` / `observation_date` / `references.prev_period` を確認し、週次の対象期間に抜けがないか見る
4. 新規 `world-weekly` を作る場合、直前の週次対象期間の翌日から始まっているか確認する
5. 欠損がある場合は、現在週を作る前に欠損週を backfill する
6. backfill 後、現在週の `references.prev_period` と前週比計算の基準を backfill した週次 brief に更新する

### 判断ルール

- `world-daily` は週次欠損を埋める代替にはしない。日次 brief が存在しても、週次 brief の対象期間が飛んでいれば `world-weekly` 欠損として扱う
- 週次欠損の backfill は `published_at` を実作成日時にし、`対象期間` は欠損していた週にする
- backfill では、その時点で確認できる一次ソースだけを使う。取れない値は `データ取得失敗` と明記する
- 欠損確認で見つけた gap を放置したまま PR / commit しない

### 確認コマンド例

```bash
find records/01-brief/2026 -type f -name '*.yaml' | sort
find records/01-brief/2026 -type f -name '*world-weekly*.yaml' | sort
for f in $(find records/01-brief/2026 -type f -name '*world-weekly*.yaml' | sort); do
  printf '\n== %s ==\n' "$f"
  rg -n 'observation_date:|period:|prev_period:|start:|end:' "$f"
done
```

## brief 作成時のデータ取得手順（FRED 経由が落ちている場合の鉄則）

brief 作成における**データ欠損は基本的に許容しない**。`データ取得失敗` 表記は最後の手段で、まずは Tier 1 / Tier 1 準拠の代替経路を試すこと。詳細な経路一覧は [data-sources.md](./data-sources.md) の「既知の取得経路と代替ルート」表を参照。

### 取得手順

1. 各指標について、まず [data-sources.md](./data-sources.md) の代替ルート表で第一経路を確認する（FRED が第一経路ではないものが多い）
2. 第一経路が落ちていれば第二経路、第三経路と試す
3. すべて失敗した場合に限り `データ取得失敗` を記録する。その際、ソース列に `(アクセス不能, YYYY-MM-DD取得試行)` を必ず併記し、何が起きたか（HTTP 403 / HTTP/2 stream error / PDF パース不能 等）を週次コメント列で 1 文書く
4. 値を埋めた指標は、ソース列にこの brief で実際に使った Tier 1 / Tier 1 準拠 URL（FRED は試行できなくても、利用した代替の H.15 / H.10 / EIA / CBOE / ECB / Wayback Machine など）を書く。FRED 直リンクをソースに残しつつ実際に取った値が別経路、という記法は禁止（再現できない）

### よくある罠

- **FRED 直リンクをソースに書いたまま、別経路で取った値を載せる**: 観測の再現性が崩れる。実際に取得したソースを書く
- **前週 brief のテンプレ文言（例: 「前週 brief 表示値 約159.5 から -0.1%」）をそのままコピーして数値だけ入れ替える**: 前週 brief で使った参照値とこの brief の参照値が一致しているか毎回確認する。コピー検出のため、前週比計算式も短く併記してよい
- **CME / Nikkei 公式 / FRED が落ちる前提でテンプレを残す**: `データ取得失敗` をそのまま流用しない。毎回再試行し、復活していないか確認する。連続 2 回ダメなら data-sources.md 側に代替経路として登録する
- **PDF を取得しただけで「取れた」と扱う**: JPX / BOJ の PDF は CMap-encoded で本作業環境では数値抽出に失敗することが多い。テキスト抽出が完了したかまで確認する

## world-daily から macro-monthly への移管

`world-daily` が月次級データを一時保持したあと、`macro-monthly` が完成したら以下で整合を取る:

1. `macro-monthly` がその月の月次級データの **正本** になる
2. 先行していた `world-daily` は削除しない。原始記録として保持し、archive 扱いにする
3. 以後の `world-daily` / `world-weekly` では同じ数値を再掲せず、該当 `macro-monthly` へのリンクで参照する
4. outlook 更新時は、通常は `macro-monthly` を canonical input とし、鮮度のために必要だった先行 `world-daily` は bootstrap / 緊急更新時の補助入力として扱う

## 事実記述の粒度

brief は事実レイヤー専用ドキュメント。解釈・予測・相場観は書かない（詳細は [design-principles.md](./design-principles.md) の「事実と分析の分離」節を参照）。

- 事実と解釈は分離し、引用時は事実記述部分に限定する
- 以下の表現は解釈・推測を含むため、地の文では原則として使わない:
  - 「観測される」「〜の観測」
  - 「〜を背景に」「背景として」
  - 「〜を受けて」「〜の影響で」
  - 「示唆する」
  - 「〜と思われる」「〜とみられる」
- 例: `Brent $98.05 に下落。CNBC は中東情勢緩和を下落要因として挙げている` は許容（因果は報道の引用として明示している）
- 例: `中東情勢緩和観測を受けて Brent が下落した` は NG（因果を地の文で推測している）

## 差分データ

週次 / 月次 brief では、観測値のスナップショットだけでなく **前期間からの変化量** を計算して記録する。

> **この節で扱うのは計算結果とルール適用による事実の整理のみ。解釈・予測・相場観（「〜を示唆する」「〜と思われる」「次は〜」等）は書かない。** brief 全体の「事実レイヤー」原則については [design-principles.md](./design-principles.md) の「事実と分析の分離」節を参照。

### 差分の観点

- **週次 brief**: 前週比 (WoW)
- **月次 brief**: 前月比 (MoM) + 前年比 (YoY)

### 2 層構造

情報の完全性（=閾値変更時も再分類できる）と可読性（=大きな変化だけを一覧できる）を両立するため、以下の 2 層で記述する:

- **層 A — 各指標行の週次コメント列**: 全指標の変化量を生のまま記録。閾値未満の小さな変化もここに残す
- **層 B — 差分データ節**: 閾値を超えた変化だけを抽出して一覧化

### 差分データの閾値（週次）

市場変動の標準偏差を超えた変化量を機械的に分類する。クラス単位で閾値を設定し、指標個別のルールを避けて簡潔に保つ:

| クラス | 代表指標 | Notable | Major | 単位 |
|---|---|---|---|---|
| 株価指数 | 日経 / TOPIX | ±1.5% | ±3% | % |
| 主要通貨ペア | USD/JPY / EUR/JPY / AUD/JPY | ±1% | ±2% | % |
| 主要国債利回り | 米 10Y / 米 2Y | ±15 bp | ±30 bp | bp |
| 主要コモディティ | Brent / WTI / 銅 / 金 | ±2.5% | ±5% | % |
| ボラティリティ指数 | VIX | ±3 pt | ±5 pt | pt |

### 差分データの閾値（月次、macro-monthly 用）

| クラス | 代表指標 | Notable | Major | 単位 |
|---|---|---|---|---|
| CPI 系 (YoY) | コア CPI / コアコア CPI | ±0.3 pt | ±0.5 pt | pt |
| CPI 系 (MoM) | CPI (MoM) | ±0.3 pt | ±0.5 pt | pt |
| 雇用関連 | 失業率 | ±0.2 pt | ±0.4 pt | pt |
| 政策金利 | FF 目標レンジ / 無担保コールレート | 変更あり (±25 bp) | — | bp |

### Major / Notable の定義

**変化量の統計的大きさのラベル**であり、**重要度の主観的評価ではない**。閾値が workflow で明示されているため、分類は機械的に実行できる。「Major」を「重要」「判断に影響する」等の意味で解釈してはならない（それは事実レイヤーを越える推論）。

### 方向履歴と方向反転

差分は点だけでなく線でも見る:

- **方向履歴**: 過去 4 週の方向を矢印列で記録（例: `↑↑↑↑`、`→↑↑↓`）
- **方向反転**: 3 週以上同方向だった後に反対方向へ動いた週を機械的に記録（例: `↑↑↑↓` の最後の週）

矢印の判定基準: `↑` = 前週比プラス、`↓` = 前週比マイナス、`→` = 前週比が層 A で計測可能な最小単位未満（実質変化なし）

**用語注意**: かつて「連続トレンド」「転換点」と呼んでいた概念を、解釈性を帯びる用語を避けて「方向履歴」「方向反転」に置き換えている。3 週連続同方向を「トレンド」と呼ぶと「トレンドが続く見込み」という含意を誘発するため、純粋な計算結果として命名し直した。

### 前週 brief の参照

- 週次 brief の本文冒頭に「前週 brief: [パス]」を記載する
- 初回週（前週 brief がない場合）は「該当なし（差分データ初回）」と明記
- 月次 brief の場合は「前月 brief」「前年同月 brief」（あれば）を同じ場所に記載

### 閾値の校正

初期閾値は FX / 株式 / 金利市場の一般的通念に基づく暫定値。**四半期ごと**（四半期末月の最終週次 brief 作成時）に過去 12 週の実データから標準偏差を算出し、Notable ≒ 1σ / Major ≒ 2σ を目安に閾値を校正する。校正結果は本ファイルの閾値テーブルを更新し、末尾の「閾値校正履歴」に記録する。

### 閾値校正履歴

- **2026-04 (初期値設定)**: 市場の一般的通念に基づく暫定値。次回校正は 2026-06 末（四半期末）。

## 前回値の扱い

- 「前回」列には前月値（月次指標）または前四半期値（四半期指標）を記載する
- 前回値も一次統計から取得する
- 予想値はエコノミスト予想の信頼できる公開一次ソースが限定的なため、テンプレートの列からは削除した。予想に言及する場合は本文中に出典を明記する

## 未発表の指標の扱い

- 観測日時点で未発表の指標は行として残し `未公表（次回予定: YYYY-MM-DD）` と記載する
- 後日確報値が出たらその時の記録に反映する。過去の記録は原則書き換えない

## データ取得失敗時の運用

「発表済みだが今回の作業環境から取得できない」ケースを `未公表` と混同しない。両者は状態が異なる:

- **未公表**: 発表主体がまだ出していない → `未公表（次回予定: YYYY-MM-DD）`
- **取得失敗**: 発表済みだが、今回の作業環境から取得できない（ネットワーク遮断・ブロッキング・一次ソースの仕様変更など） → `データ取得失敗`

### 記載ルール

- 値列: `データ取得失敗`
- ソース列: 試行した Tier 1 / Tier 2 URL を残し、取得日欄を `(アクセス不能, YYYY-MM-DD取得試行)` に置換する
- 週次コメント / 前月比列: `—` ではなく、値が欠ける旨を明示（例: `データ取得失敗（履歴 CSV アクセス不可）`, `データ取得失敗（前週値不明）`）

### 差分計算への影響

- 取得失敗の指標は閾値判定の対象外とし、差分データ節では「判定不能」と記録する
- 方向履歴は取得可能な範囲だけ作成し、不能な場合は「データ不足または取得失敗により判定不能」と明示
- 取得失敗が一時的か恒常的かは**次回 brief 作成時に再試行して確認する**

### 恒常的な取得失敗の扱い

連続 2 回の brief 作成で同じ Tier 1 ソースが取得失敗した場合、[data-sources.md](./data-sources.md) 側の運用を見直す。具体的には:

- 代替ソース（同じ一次統計を別 URL で配信しているミラー・集約サイト）の Tier 1 準拠扱いを検討する
- Tier 1 準拠扱いに追加できるものがない場合は、「Tier 1 取得試行が通らない環境では該当指標は空欄運用」を明記する
- 暫定的に Tier 2 / `[補助外]` で数値を埋めることは**行わない**（一次統計の数値は一次ソースでしか確定させない）

## テンプレート

- 日次記録を作るときは [templates/brief-world-daily.yaml](./templates/brief-world-daily.yaml) をコピーして使う
- 週次記録を作るときは [templates/brief-world-weekly.yaml](./templates/brief-world-weekly.yaml) をコピーして使う
- 月次記録を作るときは [templates/brief-japan-monthly.yaml](./templates/brief-japan-monthly.yaml) をコピーして使う
- 事実ベース運用のため、テンプレートに主観的な「解釈」「示唆」欄は設けていない

## 作成後セルフレビューチェックリスト

brief を書いた後、コミット前に以下を確認する:

- [ ] 全 citation が Tier 1 / Tier 1 準拠 / Tier 2 / `[補助外]` のいずれかに収まっているか
- [ ] 「事実記述の粒度」節の禁止表現が地の文に含まれていないか
- [ ] 未発表指標が `未公表（次回予定: YYYY-MM-DD）` 形式になっているか
- [ ] 取得失敗指標は `データ取得失敗` 表記かつソース列が `(アクセス不能, YYYY-MM-DD取得試行)` になっているか（`未公表` と混同していないか）
- [ ] 観測日 / 市場データの基準日 / `(YYYY-MM-DD取得)` が整合しているか
- [ ] 階層構造（世界情勢 → 日本経済 → 日本株）の各節が埋まっているか。埋められない項目は「該当なし」または `未公表（次回予定: YYYY-MM-DD）` と記載しているか
- [ ] 月次データを週次 brief に再掲していないか（該当月の `macro-monthly` brief を参照リンクで代替しているか）
- [ ] `macro-monthly` 未作成の月次級データを daily に置いた場合、その旨が明示されているか
