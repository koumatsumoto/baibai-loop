---
name: ai-value-bargain-selection
description: >-
  長期的に AI で企業価値が高まる × 今のトレード状態で割安になっている日本株を、
  スクリーニング基盤から TOP12 → 一次 IR 深掘り → 4 銘柄 → 最良リスクリワード 1 銘柄へ
  絞り込み、HTML レポートと PR で提案するまでの end-to-end 手順。
  「お買い得な銘柄を選定して」「新規に買う AI 割安株を選んで」「今買う銘柄を提案して」
  と言われたとき、または同種の銘柄選定を再現するときに使う。
---

# AI バリュー・バーゲン銘柄選定（Baibai-Loop）

長期 AI 構造価値 × 足元割安の日本株を、本リポジトリの screening 基盤で選定し提案する手順。`AGENTS.md` の anti-pattern（AP-01 一次情報 / AP-02 検算 / AP-09 会社 IR 確認）、`docs/portfolio-management.md`（単一プール資本・concentration cap・塩漬け耐性ゲート・割高で全売り）、`docs/doctrine.md` 柱 5（単一合成スコアを出さない＝スコアは軸別座標）、`docs/workflow/research.md`（FV・RR・期待利回りの見積り式と entry/exit 規律）に従う。

## 0. ゴールと前提

- **ゴール**: 長期的に企業価値が高まる銘柄のうち最もお買い得なものを選定し、ユーザーに提案する。最終提案はユーザーがレビューして決める（research memo の `approved` は決定後に作る）。
- **前提の固定**: 投資フレームは doctrine の long-hold value（長期積立・価格 stop なし・割高化 or 事業毀損で全売り）で固定。AI の解釈の広さ（本命AIのみ / AI受益まで広く / RR最優先で範囲不問）だけ、依頼文から読めない場合に `AskUserQuestion` で確認する。
- **除外**: 既存保有銘柄（`records/04-position/` から）は「新規」候補から外す。パチンコ機械のような構造的に廃れる事業は人間判断で外す。

## 1. 準備

```bash
# 現保有（新規候補から除外する ticker）
ls records/04-position/*/*/*.md
# データ鮮度（最新営業日 = screen の asof）
python3 -c "import sqlite3;c=sqlite3.connect('data/screening/market.sqlite');print(c.execute('SELECT MAX(traded_at) FROM jquants_daily_bars').fetchone())"
# 最新 candidates（無ければ下のパイプラインで生成）
ls records/02-candidates/*/*/*.yaml | tail -3
```

cache が最新営業日に届いていなければ、その asof まで拡張してから run する（J-Quants throttling 時は直近の完全営業日にフォールバック）。`run` / `select` は cache-only / point-in-time で、API fallback はしない:

```bash
ASOF=<最新の完全営業日 YYYY-MM-DD>
uv run baibai-loop-screening bootstrap-cache --asof "$ASOF"
uv run baibai-loop-screening extract-edinet-metrics --asof "$ASOF"   # 数分かかる（背景実行可）
uv run baibai-loop-screening verify-cache-coverage --asof "$ASOF"
uv run baibai-loop-screening run --asof "$ASOF"                      # universe→candidates
```

## 2. macro context を「深く」作る（リスクリワードの土台）

[[feedback_macro_context_depth]]: 浅い macro は不可。直近の世界情勢を入念に多角的に分析し、RR を判断できる前提にする。トークンは気にせず最大限。

- **テーマ別 subagent fan-out**（[[feedback_subagent_cap]] により**同時起動は最大 5**。多ければ wave に分ける）:
  1. AI / 半導体 / データセンター capex サイクル（拡大 or digestion かが AI-tilt の RR を左右）
  2. 米マクロ・Fed・金利・米株（Mag7 集中度含む）
  3. 日本マクロ・BOJ・JGB・USD/JPY・春闘・需給/PBR 改革
  4. 地政学・通商・半導体規制・台湾・原油
  5. クロスアセット・シナリオ（base/bull/bear/tail）・直近急落の post-mortem・invalidation
- 各 agent に: Tier-1 を多数（20+ 目安、各 URL+公表日）、確認/推定を区別、**「割安な日本 AI/DX 株を今買う RR にどう効くか」へ接続**、を要求。session 制限に備え「partial でも必ず結論を返す」と指示。
- 合成して `records/01-macro-context/YYYY/MM/macro-context-YYYY-MM-DD-<slug>.yaml` を作る。schema 必須: `kind, context_id, as_of, valid_until, published_at, summary, inputs(articles[]+indicator_series[]), sector_tilts.items[](id/scope=sector_33/key/stance∈tailwind|neutral|mixed|headwind/strength/confidence/rationale), research_questions[], refresh_triggers[], changes_since_previous[]`（additionalProperties=false）。`uv run baibai-loop-validation --target macro-context` を通す。
- `select` は macro `as_of` が candidates asof より新しいと拒否する。mechanical run には asof 以前で valid な context を使い、買い判断の深い分析は最新 context で行う。macro `as_of` は candidates asof（＝最新の完全営業日）に合わせる。
- **独立性とregime-flip**（[[feedback_macro_analysis_independence]]）: 過去の自リポジトリの「解釈・結論・tilt・建玉」は前提にせず、最新の一次情報（直近の**大引けまで**）からゼロベースで読む。過去の「事実=価格・指標・イベント」のみ前提可。regime は1日で反転しうる（例: 決算 surprise で AI 懐疑→リスクオン）。intraday に書いた context が大引けで覆ったら**書き直す**。
- **pivotal な数値は検算する**（AP-02）: 相場観の土台になる1点（例: 指数の単日 +4.6%、信用 spread、原油水準）は複数 Tier-1 で cross-check し、`baibai-loop-macro` の series でも裏取りする。1 ソースの大きな数字を鵜呑みにしない。

## 3. `select` で割安候補 TOP10 を出して人手で TOP12 を確定する

screening の正本 ranking (`select`) を最新 macro context に対して走らせ、軸別座標 (lane / lenses / market_regime / 流動性除外件数) を含む診断付き payload を取得する。AI 構造性は §4 の一次 IR 深掘りで人間判定する (scorecard / structural-outlook 系のサブシステムは前回 cleanup で削除済み)。

```bash
uv run baibai-loop-screening select --asof YYYY-MM-DD --top 10 --detail full > .cache/select-<asof>.yaml
```

- 出力 `recommendations[]` から **既存保有 ticker** と **構造衰退業種 (パチンコ機械 / 有料衛星放送 / 旧来繊維機械 / 印刷等)** を skill 側 post-filter で除外し、TOP12 候補を確定する (`select` には除外フラグはない)。`split_adjustment_recent` risk tag が付く候補は market_cap / net_cash 比率が corporate action 未反映で歪み得るため、一次 IR で株数基準を必ず検算する (AP-03)。
- `select` は valuation-reversion / cash-rich-asset-discount / cashflow-yield-discount / sales-discount-growth の 4 screen を `evidence_hits` で示し、`selection.diagnostics.market_regime` で benchmark trend (fact annotation) を返す。表示順は playbook 優先順 × valuation discount の ranking で verdict ではない。recommendations は config の `research_selection_target_max`(5) で cap されるため、広い triage には `--rules-path` で一時 rules を渡す。
- 候補に厚みが必要なら `--top 20` まで広げて post-filter 後に 12 件を確保する。
- **補完スキャン**: `select` の recommendations は高 precision ゆえ薄い / テーマ（AI/DX）に偏らないことがある。その場合は candidates YAML 全体を直接走査し「AI/DX 関連 sector × 3<PER<14 × net_cash/mc>0.20 × ocf_yield>0.08 × 自己資本>0.5 × op_yoy>-0.10」等で本命候補を補完して TOP12 に繰り上げる。select の forward-measured ランキングを core、補完スキャンを enrich とし、両者を IR で検証する。

## 4. TOP12 を一次 IR 深掘り（≤5 subagent / wave）

- **同時 5 まで**。12 銘柄なら 2-3 銘柄/agent × wave で回す。各 agent に `km:ir-research` の規律（一次/準一次 2 ソース検算、決算期・分割の取り違え厳禁、見出しの罠回避）を要求。
- 各銘柄で出す: 事業/売上構成、**AI/DX 構造性の本物度（具体的製品で懐疑的に。後付けを見抜く）**、長期見通し、直近通期+来期予想、valuation、**なぜ今割安/下落したか（決算ミス / ガイダンス減 / 需給 de-rating / 全体安の切り分け）**、財務/下値（net cash・営業CF・自己資本比率）、株主還元、リスク/invalidation、総合判定（AI 長期価値 × 割安度 × RR）。出典 URL と確度を必須に。
- 返ってきた数値は candidate row（2026-06-12 等）や EDINET 値と相互検算する。

## 5. 4 銘柄に絞り、最良リスクリワード 1 銘柄を選ぶ

- 軸で横並び比較（単一合成スコアに畳まない）。重視: **AI 構造性の確度（後付けでない）× 割安度（de-rating であって業績崩壊でない）× 下値保護（net cash/CF/還元）× 近接 catalyst × 長期保有の質（塩漬け耐性）**。
- **value-trap は forward-quality ゲートで弾く**（本フローの最重要精度レバー）: trailing が割安でも「来期(FY+1)の減益ガイダンス or ガイド非開示」「op が伸びても FCF≈0/低 cash 変換」「PER は安いが EV/EBITDA は割高」「ピーク循環（単一製品・単一顧客依存の業績ピーク）」は value trap として減点。減配・規制 overhang・のれん減損リスクも同様。AI ラベルが最弱セグメントに偏在する銘柄は本物度を下げる。これらの判別シグナル（per/pbr/ev_ebitda/p_s/pcfr/cash・net_cash/equity/ocf/operating_profit_yoy/sales_yoy/fcf_yield）は `select` の recommendation 出力に転記済みで、ticker-profile を別途引かずに triage できる。
- 「割安の理由」は **de-rating（需給・全体安・中計未達などで株価が崩れたが業績は崩壊していない）と earnings-collapse（業績そのものが崩れている）を切り分ける**。買うのは前者。
- 4 銘柄 + 最良 1 銘柄を確定し、各々に **FV・想定下値・invalidation_conditions・durability_gate** と policy 準拠の sizing（`src/baibai_loop/position/policy.py` の `PORTFOLIO_POLICY`: 単一プール real_capital ¥10,000,000・ticker cap 6%=¥600,000・sector 40%・playbook 35%・ADV 5%・board lot 100。1 注文の絶対額上限は無く月次予算 ¥20–30 万で律速）を付す。`expected_upside=(fair_value/entry-1)*100`、`expected_downside=保守下値までの判断値`、`RR=upside/downside ≥ 2 目安`、`expected_yield=FV 収束の年率 + 配当`（AP-02 で検算。式の正本は docs/workflow/research.md）。
- **具体的な指値プラン**: entry は最新終値基準の指値（laggard を強さに追わないなら終値のわずか下）。board-lot 丸めで月次予算・ticker cap 内に収め、「約定しない場合」のルール（押し目待ち等）と binary event（NFP/FOMC/BOJ/決算）を跨がないタイミングも書く。
- **RR は market regime で調整する（最重要・甘くしない）**: `target÷stop` のボトムアップ RR は<strong>ベストケース</strong>。市場が最高値圏（regime=risk_on_rally かつ指数が ATH 圏）なら、(a) 上方は限定的・低確率（バリュエーション過熱・mean-reversion）、(b) 下方はテール厚め（Bear/Tail、単日ギャップでキャリー巻戻し −10%+）として **upside/downside をシナリオ別・β調整・ギャップ込みで引き直す**。価格 stop は置かない前提（long-hold）に立ち、実質の下値境界は<strong>ネットキャッシュ/ファンダ床</strong>（net cash/株 ＋ distressed 事業価値。ただし還元・実現機構が確認できる場合に限り床扱い、docs/workflow/research.md）に置く。確率加重の期待リターンと分布の歪み（左テール）も出す。**最高値圏では「待つ／小さく段階建て」が最良 RR のことが多い**。RR を綺麗な単一倍率で誇張しない。

## 6. HTML レポート + PR で報告

- **HTML レポート**: `km:html-document` で 1 枚物の HTML を作る（内容＝市場 context の深い分析・4 シナリオ・主要リスク・select 軸別座標・4 候補比較・最良 RR の根拠・**翌営業日以降の具体的指値**・**リスクリワードの正直な評価（必須・下記）**・一次ソース。skill はレイアウト/セキュリティのみ担当）。`reports/YYYY-MM-DD-ai-value-bargain-selection.html` に保存して **commit する**（root の `/baibai-loop-*.html` は gitignore 対象なので `reports/` 配下に置く）。
  - **「リスクリワードの正直な評価」は毎回必須セクション**: ボトムアップ RR（target÷stop）はベストケースと明記し、market regime（最高値圏か）でテール・ギャップ・β調整した**上昇余地と下落余地**、ネットキャッシュ/ファンダ床、確率加重期待リターンと分布の歪み（左テール）を出す。RR を綺麗な単一倍率で誇張しない（§5 の RR 規律を結果に必ず反映する）。
- **PR で添付**: `km:github-workflow` で PR を作り、commit 済み HTML レポートを PR の差分に含める（＝添付）。PR body には 4 候補・最良 1・entry/target/stop/invalidation・sizing・主要リスクの markdown サマリを self-contained に書く（GitHub 上で読めるよう、レポートのリンクだけに依存しない）。必要なら proposal Issue も併設する。

## 7. 検証・PR

基盤コードや records を触ったら commit 前に通す:

```bash
uv run baibai-loop-validation && uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest
```

1 issue = 1 PR、commit で分ける（[[feedback_pr_splitting]]）。基盤変更と選定成果物・docs を同一 PR に積む。

## 8. 完了条件

- AI 構造性が本物で、足元 de-rating で割安、下値保護のある銘柄を、一次 IR 出典つきで 4 つに絞り、最良 RR 1 つを根拠つきで選んだ。
- 深い macro context（多角・Tier-1 多数・シナリオ・リスク）が RR の前提として揃い、validate を通った。
- **リスクリワードを regime 調整・ギャップ込み・ファンダ床で正直に評価し、結果（レポート・提案）に必ず含めた**（ボトムアップの単一倍率で誇張していない。最高値圏なら「待つ／小さく段階建て」の選択肢も提示した）。
- HTML レポートと PR でユーザーがレビューできる形になっている。検証（validate/ruff/mypy/pytest）が緑。

## 9. 制約（必ず守る）

- **サブエージェント同時起動は最大 5**（[[feedback_subagent_cap]]）。多数対象は wave 化。
- **AI 期待を単独の採用 / sizing / macro fit / validator rule / ranking sort-key にしない**（`docs/doctrine.md` 柱 2）。AI 構造性は §4 の一次 IR 深掘りで人間判定する。
- **単一の合成スコア・売買指示を出さない**。スコアは軸別座標（`docs/doctrine.md` 柱 5）。
- 既存保有と構造衰退（パチンコ機械等）は新規候補から外す。
- 最終採用判断はユーザー。提案は PR + HTML レポート（必要なら proposal Issue も）で渡し、`approved` research memo は決定後に作る。

## 10. 改善しながら最後に取り込む（運用知見）

このフロー自体を高精度化するための実践知。マクロ分析→screening→選定→調査→提案を回す中で気づいた基盤の不便・不足を、現作業に混ぜず最後に同一 PR へ取り込む。

- **改善は .plan にメモ → 同一 PR に実装**（[[feedback_same_pr_for_followups]] / [[feedback_stacked_commits]]）: 運用中に見つけた基盤の不足（出力に欲しい指標が無い・sort bug・指標欠落・test の外部依存 等）は現作業に混ぜず `.plan` にメモし、選定が終わったら同じ PR に実装で積む。
  - 各改善は **forward 計測経路を1行で説明できること**（計器原則 / AGENTS.md AP-08）。説明できない改善は入れない。**test でロック**する。
  - **監査専用 logic は足さない / 後方互換は気にしない**（CLAUDE.md 優先度ルール）。古い実装はまず捨てる。
  - 実例: select recommendation への valuation / 下値保護 / value-trap シグナル転記（ticker-profile 往復を不要にし triage を速く・確実に）。macro 回では observation sort 正常化・米株指数/CCC OAS 追加・unit test の J-Quants 依存と time-bomb の除去。
- **改善の発見源は「自分が手で補った所」**: 選定中に CLI 出力だけでは足りず手作業（別コマンド・手計算・全候補スキャン）で補った箇所こそ、基盤に載せるべき改善。今フローの最大レバーは「value-trap 判別に必要な forward-quality 指標（op_yoy/fcf_yield/EV 倍率/来期ガイド）を select 出力に載せる」だった。
- **計測・検証の規律**: pivotal な数値は複数 Tier-1＋`baibai-loop-macro` で検算（AP-02）。unit test は外部 API を呼ばない（fixture/モックで完全オフライン、forward window は now 基準で time-bomb 回避）[[feedback_unit_test_no_external_api]]。サブエージェントは同時最大 5、制限時はメインコンテキストの WebSearch で代替 [[feedback_subagent_cap]]。
- **最後に適用する成果物**: 改善（code+test）＋選定 HTML レポート＋この skill の更新を、運用テストで通した全 gate（validate/ruff/mypy/pytest）green の状態で 1 PR にまとめ、`km:review` で深くレビューしてから提出する。
