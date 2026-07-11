---
name: ai-value-bargain-selection
description: >-
  長期的に AI で企業価値が高まる × 今のトレード状態で割安になっている日本株を、
  スクリーニング基盤から TOP12 → 一次 IR 深掘り → 4 銘柄 → 最良リスクリワード 1 銘柄へ
  絞り込み、canonical packetと短い第1層proposalを人間判断へ渡すまでの end-to-end 手順。
  「お買い得な銘柄を選定して」「新規に買う AI 割安株を選んで」「今買う銘柄を提案して」
  と言われたとき、または同種の銘柄選定を再現するときに使う。
---

# AI バリュー・バーゲン銘柄選定（Baibai-Loop）

> **正本と操作の分離**: 本 skill は選定フローの〈操作〉（漏斗の順序・判断ノブ・出力形態）を持つ。思想・仕様・契約の正本は docs 側にあり、本 skill はそれを書き写さず参照する — [`doctrine.md`](../../../docs/doctrine.md)（柱 2 / 柱 5）・[`portfolio-management.md`](../../../docs/portfolio-management.md)（資本・cap・耐性ゲート）・[`workflow/screening.md`](../../../docs/workflow/screening.md)・[`workflow/research.md`](../../../docs/workflow/research.md)（FV・RR・見積り式）・[`reference/decision-packet.md#execution-pricing`](../../../docs/reference/decision-packet.md#execution-pricing)（最大許容価格と指値policy）・[`operations/decision-cycle.md#2-opportunity-path`](../../../docs/operations/decision-cycle.md#2-opportunity-path)（triggerとproposal導線）。

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
# 最新 candidates（無ければ pipeline で生成）
ls records/02-candidates/*/*/*.yaml | tail -3
```

cache が最新営業日に届いていなければ、その asof まで拡張してから run する。screening pipeline（`bootstrap-cache` → `extract-edinet-metrics` → `verify-cache-coverage` → `run`）を実行するtriggerは [`docs/operations/decision-cycle.md#2-opportunity-path`](../../../docs/operations/decision-cycle.md#2-opportunity-path) を正本にする。**skill 固有ノブ**: `ASOF` は最新の完全営業日を使い、J-Quants throttling 時は直近の完全営業日へフォールバックする。`run` / `select` は cache-only / point-in-time で API fallback しない（`extract-edinet-metrics` は数分かかるので背景実行可）。

## 2. macro context を「深く」作る（リスクリワードの土台）

作成手順・8 レンズ・パネル取得・独立性 / regime-flip・数値検算・公開前の敵対的 self-check は skill [`macro-analysis`](../macro-analysis/SKILL.md)（正本 [`docs/workflow/macro.md`](../../../docs/workflow/macro.md)）に従い、成果物 `records/01-macro-context/YYYY/MM/macro-context-YYYY-MM-DD-<slug>.yaml` を `uv run baibai-loop-validation --target macro-context` で通す。本フロー固有に効く制約だけをここに残す。

- **深さ基準**（[[feedback_macro_context_depth]]）: 浅い macro は不可。直近の世界情勢を入念に多角的に分析し、RR を判断できる前提にする。各テーマで Tier-1 を多数（20+ 目安・各 URL+公表日）引き、**「割安な日本 AI/DX 株を今買う RR にどう効くか」へ必ず接続**する。テーマ別 fan-out の目安:
  1. AI / 半導体 / データセンター capex サイクル（拡大 or digestion かが AI-tilt の RR を左右）
  2. 米マクロ・Fed・金利・米株（Mag7 集中度含む）
  3. 日本マクロ・BOJ・JGB・USD/JPY・春闘・需給/PBR 改革
  4. 地政学・通商・半導体規制・台湾・原油
  5. クロスアセット・シナリオ（base/bull/bear/tail）・直近急落の post-mortem・invalidation
- **fan-out 制約**（[[feedback_subagent_cap]]）: サブエージェント同時起動は最大 5。多ければ wave に分け、各 agent に「partial でも必ず結論を返す」と指示する。
- **select との鮮度整合**: `select` は macro `as_of` が candidates asof より新しいと拒否する。mechanical run には asof 以前で valid な context を使い、`as_of` は candidates asof（＝最新の完全営業日）に合わせる。買い判断の深い分析は最新 context で行う。

## 3. `select` で割安候補 TOP10 を出して人手で TOP12 を確定する

screening の正本 ranking (`select`) を最新 macro context に対して走らせ、軸別座標 (lenses / market_regime / 流動性除外件数) を含む診断付き payload を取得する。AI 構造性は §4 の一次 IR 深掘りで人間判定する (機械の合成スコアは存在しない)。

```bash
uv run baibai-loop-screening select --asof YYYY-MM-DD --top 10 --detail full > .cache/select-<asof>.yaml
```

- 出力 `recommendations[]` から **既存保有 ticker** と **構造衰退業種 (パチンコ機械 / 有料衛星放送 / 旧来繊維機械 / 印刷等)** を skill 側 post-filter で除外し、TOP12 候補を確定する (`select` には除外フラグはない)。`split_adjustment_recent` risk tag が付く候補は market_cap / net_cash 比率が corporate action 未反映で歪み得るため、一次 IR で株数基準を必ず検算する (AP-03)。
- `select` は liquidity を通過し E[r] が非 null の母集団を機械 E[r] (成分分解付き年率見積り) の降順で並べ、valuation-reversion / cash-rich-asset-discount / cashflow-yield-discount / sales-discount-growth の 4 screen は `evidence_hits` と `selection_playbook` で thesis annotation として示す。evidence がない候補も、E[r] 上位なら `selection_playbook: null` のまま recommendation に入る。`selection.diagnostics.market_regime` は benchmark trend (fact annotation) を返す。表示順は verdict ではなく、E[r] 成分 (reversion/carry) と FV アンカー (`fv_sector_median_yen` / `fv_self_range_yen`) が recommendation に転記されるので、FV 見積りの出発点にする。recommendations は config の `research_selection_target_max`(5) で cap されるため、広い triage は `--top` だけでなく、本番 rules YAML の `.cache` コピーで `output.research_selection_target_max` を引き上げ、その一時 rules を `--rules-path` で渡す。本番 rules と calibration store は、較正済み baseline と本番順位の再現性を保つために触らない。
- 候補に厚みが必要なら `--top 20` まで広げ、`research_selection_target_max` も一時 rules 側で同じ深さに広げて post-filter 後に 12 件を確保する。
- `dps_actual_annual / dps_forecast_annual > 1.5` の候補は、E[r] carry を予想配当基準で読み替える。IR 対象化の前に、株式分割・併合などの corporate action（AP-03）、特別配当、減配ガイダンスを一次 IR / 適時開示で確認し、実績配当と予想配当の乖離が持続的な carry ではない可能性を潰す。
- **補完スキャン（条件付き・多くの場合は不要）**: `select` は E[r] 降順で `--top 20` まで広げれば TOP12 の core が通常足りる。補完スキャンが要るのは **recommendations が薄い / AI・DX テーマ特化で厚みが不足するとき**だけ。その場合に限り candidates YAML 全体を直接走査し「AI/DX 関連 sector × 3<PER<14 × net_cash/mc>0.20 × ocf_yield>0.08 × 自己資本>0.5 × op_yoy>-0.10」等で本命候補を補完して TOP12 に繰り上げる。select の forward-measured ランキングを core、補完スキャンを enrich とし、両者を IR で検証する。

## 4. TOP12 を一次 IR 深掘り（≤5 subagent / wave）

- **同時 5 まで**。12 銘柄なら 2-3 銘柄/agent × wave で回す。各 agent に `km:ir-research` の規律（一次/準一次 2 ソース検算、決算期・分割の取り違え厳禁、見出しの罠回避）を要求。
- 各銘柄で出す: 事業/売上構成、**AI/DX 構造性の本物度（具体的製品で懐疑的に。後付けを見抜く）**、長期見通し、直近通期+来期予想、valuation、**なぜ今割安/下落したか（決算ミス / ガイダンス減 / 需給 de-rating / 全体安の切り分け）**、財務/下値（net cash・営業CF・自己資本比率）、株主還元、リスク/invalidation、総合判定（AI 長期価値 × 割安度 × RR）。出典 URL と確度を必須に。
- 返ってきた数値は candidate row（2026-06-12 等）や EDINET 値と相互検算する。

## 5. 4 銘柄に絞り、最良リスクリワード 1 銘柄を選ぶ

- 軸で横並び比較（単一合成スコアに畳まない）。重視: **AI 構造性の確度（後付けでない）× 割安度（de-rating であって業績崩壊でない）× 下値保護（net cash/CF/還元）× 近接 catalyst × 長期保有の質（塩漬け耐性）**。
- **value-trap は forward-quality ゲートで弾く**（本フローの最重要精度レバー）: trailing が割安でも「来期(FY+1)の減益ガイダンス or ガイド非開示」「op が伸びても FCF≈0/低 cash 変換」「PER は安いが EV/EBITDA は割高」「ピーク循環（単一製品・単一顧客依存の業績ピーク）」は value trap として減点。減配・規制 overhang・のれん減損リスクも同様。AI ラベルが最弱セグメントに偏在する銘柄は本物度を下げる。これらの判別シグナル（per/pbr/ev_ebitda/p_s/pcfr/cash・net_cash/equity/ocf/operating_profit_yoy/sales_yoy/fcf_yield）は `select` の recommendation 出力に転記済みで、ticker-profile を別途引かずに triage できる。
- 「割安の理由」は **de-rating（需給・全体安・中計未達などで株価が崩れたが業績は崩壊していない）と earnings-collapse（業績そのものが崩れている）を切り分ける**。買うのは前者。
- 4 銘柄 + 最良 1 銘柄を確定し、各々に **FV・想定下値・invalidation_conditions・durability_gate** とpolicy準拠のsizingを付す。cash、concentration、board lot、ADVはcanonical ledgerとexecution policyから導出し、ここで月次予算や独自capを再定義しない。`expected_upside=(fair_value/entry-1)*100`、`expected_downside=保守下値までの判断値`、`RR=upside/downside ≥ 2 目安`、`expected_yield=FV 収束の年率 + 配当`（AP-02 で検算。式の正本は docs/workflow/research.md）。
- **具体的な指値プラン**: #341のactive lifecycle移行後だけ、5年base scenarioと要求CAGRから最大許容価格を再計算し、current quoteとcanonical ledgerを`baibai-loop-decision --execution-input ... --ledger records/04-position/portfolio-ledger.yaml`へ渡して`buy_now / shallow_limit / deep_limit / defer`を比較する。移行前はpacket / reviewまでで`defer`し、cashやexecutionを推測しない。board-lot・dry powder・max priceを満たすproposalだけを提示し、約定確率を推測しない。
- **RR は market regime で調整する（最重要・甘くしない）**: `target÷stop` のボトムアップ RR は<strong>ベストケース</strong>。市場が最高値圏（regime=risk_on_rally かつ指数が ATH 圏）なら、(a) 上方は限定的・低確率（バリュエーション過熱・mean-reversion）、(b) 下方はテール厚め（Bear/Tail、単日ギャップでキャリー巻戻し −10%+）として **upside/downside をシナリオ別・β調整・ギャップ込みで引き直す**。価格 stop は置かない前提（long-hold）に立ち、実質の下値境界は<strong>ネットキャッシュ/ファンダ床</strong>（net cash/株 ＋ distressed 事業価値。ただし還元・実現機構が確認できる場合に限り床扱い、docs/workflow/research.md）に置く。確率加重の期待リターンと分布の歪み（左テール）も出す。**最高値圏では「待つ／小さく段階建て」が最良 RR のことが多い**。RR を綺麗な単一倍率で誇張しない。

## 6. Short proposal and human decision

- canonical decision packet、independent review、source、全scenario、全risk axis、全price optionを詳細層として保存する。
- 第1層はticker / name / as-of、recommendation / confidence、5y base CAGR、3y sanity、permanent-loss conclusion、strongest countercase、max acceptable price、tactic、quantity / notional / expiry、cash / reservation / dry-powder / concentration warning、`approve / defer / reject`だけに絞る。tickerにはTradingView linkを添える。
- 通常proposalにHTML、全候補の中間比較、長い思考過程を重複して作らない。人間の判断後にだけ、exact packet hashをuser decisionへ束縛する。

## 7. 検証

基盤コードや records を触ったら commit 前に通す:

```bash
uv run baibai-loop-validation && uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest
```

基盤変更は別issue / PRへ分け、選定の判断artifactと混ぜない。

## 8. 完了条件

- viableなresearch候補がある場合、AI 構造性が本物で、足元 de-rating で割安、下値保護のある銘柄を、一次 IR 出典つきで 4 つに絞り、最良 RR 1 つを根拠つきで選んだ。候補がなければ、短い理由を残して正常終了する。
- 深い macro context（多角・Tier-1 多数・シナリオ・リスク）が RR の前提として揃い、validate を通った。
- **リスクリワードを regime調整・ギャップ込み・ファンダ床で正直に評価し、第1層proposalと詳細層の両方から確認できる**（ボトムアップの単一倍率で誇張していない。最高値圏なら「待つ／小さく段階建て」の選択肢も提示した）。
- 第1層proposalでユーザーが判断を開始でき、詳細層のpacket / review / sourceへ遡れる。検証（validate/ruff/mypy/pytest）が緑。

## 9. 制約（必ず守る）

- **サブエージェント同時起動は最大 5**（[[feedback_subagent_cap]]）。多数対象は wave 化。
- **AI 期待を単独の採用 / sizing / macro fit / validator rule / ranking sort-key にしない**（`docs/doctrine.md` 柱 2）。AI 構造性は §4 の一次 IR 深掘りで人間判定する。
- **単一の合成スコア・売買指示を出さない**。スコアは軸別座標（`docs/doctrine.md` 柱 5）。
- 既存保有と構造衰退（パチンコ機械等）は新規候補から外す。
- 最終採用判断はユーザー。proposalはdecision cycleの第1層で渡し、`approved` research memoは決定後に作る。

## 10. Improvement handoff

選定中に手作業で補った不便や、出力・指標・testの不足を見つけたら、選定成果物へ混ぜずissue化して[`operations/improvement-loop.md`](../../../docs/operations/improvement-loop.md)へ渡す。改善はforward計測経路を説明でき、testで固定できるものだけを採用する。
