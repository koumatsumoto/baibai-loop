# Macro World Model Stage A preregistration

価値tier: T2 — horizon・競合仮説・機構の誤認を publish 前に露出させ、統合済み prose が作る false narrative を止める。

## 対象と事前仮説

Stage A は production schema を変えず、full revision（prior-blind）の world model workspace から同一 session で v4 を導出する。v4 は publish を継続する。baseline audit により、比較対象は「羅列」ではなく R2–R4 の統合済み prose とする。期待する限界利益は、時間構造、競合仮説、edge の反証可能性、誤差帰属である。update locality と delta revision は評価しない。

## Hard fails

次の 1 件でもあれば pilot artifact は不合格とする。

1. key judgment が単一 indicator の言い換え。
2. key judgment に horizon / state / subgraph / counter hypothesis のいずれかがない。
3. edge に relation kind / claim strength / sign / lag / current-cycle evidence / observable falsifier がない。
4. identified でない relation を断定的因果として書く。
5. material conflicting evidence が resolution / scenario / unresolved tension のいずれにも入らない。
6. scenario が shock / persistence / propagation / reaction のいずれでも baseline と異ならない。
7. future leakage がある。
8. replay quality を申告しない。
9. policy-sensitive path に policy reaction がない。
10. stock-flow / nominal-real / level-momentum / expectation-observation を混同する。
11. material evidence candidate を理由なく除外する。
12. counter evidence を選択的に落とす。
13. prior model を blind freeze 前の workspace input に混ぜる。
14. freeze 後の prior 参照による変更を revision diff に帰属できない。
15. coverage のためだけの orphan paragraph が one-page view にある。
16. key judgment / edge を更新し得る series または公表 event を名指しできない。

issue §10.2 の「新 release で何が変わるか追跡できない」は 16 として保持する。ただし delta update や locality KPI を要求せず、full revision の falsifier / signpost が観測可能であることだけを要求する。baseline audit で substantive な counter evidence が既に存在すると確認したため、反証の「存在」ではなく competing hypothesis と common matrix への束縛を検査する。

## 昇格判定

Stage A 全体の昇格判断は結果後に緩めない。今回の第 1 サイクルでは昇格判断を行わない。

1. hard fail 0。
2. one-page view だけから central 6 questions に回答可能。
3. 所有者の blind 読み比べで「次の実 OP3 判断にはこちらを使いたい」と判定。
4. wall-clock・token・pass 数が下記予算内。
5. 独立 AI reviewer 1–2 名が hard-fail 監査を行い、blocker がない。優劣スコアの多数決はしない。

## コスト予算

第 1 サイクルは実測 baseline とする。現行 v4 の pass 別計測値が残っていないため、推測値と比較すると判定を歪めるからである。pass は charter / data audit / coverage scan / evidence packs / states / hypotheses / matrix / baseline-scenarios / graph / coherence-challenge / v4 derivation / publish の 12 区分を上限とし、各区分の wall-clock を記録する。手戻りは元の pass に加算し、別 pass に数え替えない。token は取得可能な場合だけ記録する。

cycle 2 以降の事前予算は、第 1 サイクルの world-model authoring 実測（publish / cloud 待ちを除く）を `B` とし、wall-clock `min(2B, B + 4h)`、pass 数 12 以内とする。第 1 サイクル自体は runaway stop として authoring 10 時間、pass 数 12 を上限にする。超過は hard fail ではなく stop criteria の評価材料とし、理由なく続行しない。

## Stop criteria

- ontology / graph 維持時間が分析時間を上回る。
- edge が一般論 template になり current evidence を識別しない。
- role 分離後も integrator が全 raw を再読しないと統合できない。
- one-page を含む report が長くなり central 6 questions の理解が改善しない。
- live blind evaluation で統合済み v4 を上回らない。
- Evidence Snapshot が L1 history を複製する第二の data warehouse へ肥大化する。
- historical replay を使う局面で replay quality が評価に耐えない。

issue §10.7 の update locality 条件は除外する。cadence は full revision のみで、revision 間の局所更新を目的にしない。

## 第 1 サイクルの固定条件

- `as_of`: cloud machine store を pull 後、市場データの最終完全営業日を確定する。
- horizon: now / 0–3m / 3–12m / 12–24m。
- central questions: issue §4 の 6 問。
- standing coverage: global、US、China proxy basket、Euro、Japan、cross-cutting financial conditions。
- structural exposure questions: 円、JGB / discount rate、米需要 / AI capex、中国 proxy 需要、energy。
- graph cap: nodes ≤ 20、edges ≤ 30。
- world model probability: relative plausibility の順序のみ。v4 導出時に 0.05 刻みの probability trio を付け、順序矛盾を拒否する。
- external API: unit test では呼ばない。実サイクルの source 取得だけに使う。
- promotion / history episode / Stage B mutation harness: 今回は実施しない。
