# screening/failure-taxonomy.md

採用 trade の失敗分類。`records/06-reviews/` の個別 review と月次 retro で使う。Decision lifecycle における feedback loop の質を担保するための分類体系。

## 1. 設計思想

- **固定 6 種 + 自由記述必須**: 既知失敗パターンだけで分類すると未知失敗を取りこぼす。自由記述で救う
- **四半期ごとに再分類候補を検討**: 自由記述の頻出キーワードを集約して、新カテゴリ or カテゴリ再定義の判断
- **ルール違反は別枠**: 運用の不備であり、playbook の不備ではない。playbook 改訂の input にしない

## 2. 固定 6 分類

### 2.1 材料誤読

- **定義**: 一次材料の解釈が誤っていた（採用時点で正しく読めていなかった）
- **典型例**:
  - 上振れが一事業限定だったが、会社全体と誤読
  - 前回同種材料で不発だったことを見落とし
  - ガイダンスの条件付き数値を無条件と誤読

### 2.2 既に織り込み済み

- **定義**: 採用時点で市場がすでに織り込んでいた
- **典型例**:
  - 決算発表前に上振れ期待で株価が既に上昇
  - catalyst が市場に広く認識されていた
  - 同業種で同種材料が先行していた

### 2.3 マクロ逆風

- **定義**: Macro gate 判定の誤り、または gate が保有期間中に反転
- **典型例**:
  - 採用時 `tailwind` 判定だったが、その後 BOJ / FOMC で trend が変わった
  - outlook の更新遅れで実態と乖離
  - 業種 RS が急変、業種全体の売りに巻き込まれた

### 2.4 混雑（positioning / liquidity）

- **定義**: 空売り残高・日々公表信用・特別注意・出来高不足など positioning / liquidity risk が顕在化
- **典型例**:
  - 空売り残高増加で踏み上げを狙ったが失敗
  - 日々公表信用に指定されて売買制限
  - 特別注意銘柄化で流動性急落

### 2.5 流動性不足

- **定義**: 想定より出来高が伴わず entry / exit が困難
- **典型例**:
  - entry で指値が約定しづらい
  - exit で想定価格を下回る約定
  - 取引時間外の spread 拡大

### 2.6 ルール違反

- **定義**: playbook / kill switch / position sizing 等のルール違反
- **典型例**:
  - 決算またぎをしてしまった
  - BOJ 前日 entry
  - independent evidence path が 1 つだけなのに 2% position を採用
  - Position size が上限超え
- **扱い**: 赤ラベルで識別、**playbook 改訂の input にしない**（運用の不備）

## 3. 自由記述必須

- 6 分類のどれを選んでも、review の `free_text` 欄に 1 行必ず記述する
- 自由記述は四半期 retro で読み返し、頻出キーワードを 3-5 個抽出
- 新カテゴリ提案 or 既存カテゴリ再定義の input に使う

## 4. 四半期再分類

- **タイミング**: 四半期末（3 月・6 月・9 月・12 月）の月次 retro 時
- **作業**:
  1. 過去 3 か月の全 review の `free_text` を抽出
  2. 頻出キーワードを 3-5 個洗い出す
  3. 既存 6 分類でカバーできない失敗パターンがあれば、新分類候補として記録
  4. 新分類の必要性が 2 四半期連続で確認されれば、7 番目以降として正式追加
- 正式追加時は本ファイル（`failure-taxonomy.md`）を更新し、過去 review の再分類は行わない（歴史的記録として保持）

## 5. 反対仮説カテゴリの四半期再分類

research 段階の反対仮説（8 例示 + 自由記述）も同じ方式で四半期再分類する。失敗分類と反対仮説の両方で再分類を実施することで、**採用前の仮説と採用後の失敗の両面から** 学習信号を収集する。

## 6. 成功分類（補足）

失敗だけでなく成功要因も分類する（[`../components/reviews.md`](../components/reviews.md) 参照）:

- 仮説的中
- catalyst 反応
- macro tailwind
- timing 一致

成功分類も同じ方式で四半期ごとに拡張可能とする。

## 7. 参考

- [`principles.md`](./principles.md): スクリーニング原則
- [`../components/reviews.md`](../components/reviews.md): review / retro 運用
- [`../templates/review.md`](../templates/review.md): 個別 review template
- [`../templates/retro-monthly.md`](../templates/retro-monthly.md): 月次 retro template
