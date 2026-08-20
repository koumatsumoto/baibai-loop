# 選定厳格性の再点検 — proposal ゼロ 5 週間の所在特定

価値 tier: T1 — 「保守的すぎて機会を逃していないか」を、E[r]・閾値・研究層・手順の各層に分解し、どの層が実際に binding かを実データで決める。

対象: 2026-08-20 のオーナー問題提起（「直近 proposal まで進んだものがない。E[r] の計算や閾値設定が保守的になりすぎていないか」）。前身は [2026-08-06 bargain-capture 診断](../2026-08-06-bargain-capture-diagnosis/report.md)（以下「8/6 診断」）で、本 report はその凍結基準からの差分計測である。8/6 診断 §10 は「**starter 約定が 0 件のままなら『帯を開いても通らなかった』という結論であり、band の条件側を見直す**」を事前登録しており、本再点検はその trigger の発火に相当する。

## 0. 判定

```yaml
required_return_is_binding: not_supported        # 8.5→7.5% へ下げても reject は 1 件も反転しない
upstream_er_is_binding: not_supported            # 8/6 診断を再確認。rank 選抜のため level 過小は選定を変えない
research_multiple_and_growth_stack: confirmed    # machine E[r] → research base の haircut 中央値 -4.2pt
diagnosis_revision2_never_proceduralized: confirmed  # band 文脈の人間裁定が skill に存在しない
starter_band_starved_by_flow_throttle: confirmed # 帯導入後の適格 lane は 0 件（帯の問題でなく流量の問題）
proposal_last_mile_never_exercised: confirmed    # proposal table は累計 0 行。7/15 の実買いも ledger 直接
realized_cost_concentrated_in_high_base_defers: confirmed  # +21.1% / +21.2% の 2 lane。低 base reject 群は横ばい
market_supply_genuinely_thin: supported          # panel hurdle 超え 5 件 = p16。ただし top-5 E[r] は p56 で座標は割れる
```

## 1. 再現手順

```bash
# thesis 全件の entry / FV / required / base5 / machine E[r] / 8-20 終値
python3 - <<'PY'
import sqlite3, json
c = sqlite3.connect("file:stores/application/baibai.sqlite?mode=ro", uri=True)
for tid, t, rec, pub, pl in c.execute(
    "SELECT thesis_id, ticker, recommendation, published_at, payload FROM thesis ORDER BY published_at"):
    d = json.loads(pl); est = d.get("estimates") or {}
    print(pub[:10], t, rec, est.get("entry_price_basis_yen"), est.get("current_fair_value_yen"),
          est.get("required_5y_base_cagr_pct"),
          (d["input_snapshot"].get("screening_estimate") or {}).get("expected_return_annual_ratio"))
PY
# shortlist 別 selected 数 / disposition 別 forward は §4・§7 の query を実行
# 較正 band の予実は research prepare が comparison へ焼き込む er_realized_distribution_context を読む
```

計測日 2026-08-20（forward の終端は 8/20 終値）。market store は当日 release `20260820T…`（8/20 bar 含む）。

## 2. 8/6 診断（凍結基準）から動いた事実

| 8/6 時点 | 8/20 時点 |
| --- | --- |
| research 11 lane の 5y base 2.43〜9.88%、実質全件棄却 | lane 総数 23（opportunity 13 revision / 8 ticker）。**8/7 以降の新規 opportunity lane は 6417（5.35%）と 3836（11.39%）の 2 本だけ** |
| 改訂 2「band 実現中央値を並べ、8.5% を素で当てる運用から人間裁定へ」採択 | **data 面のみ実装**（`er_realized_distribution_context` が comparison へ焼き込まれる）。research skill・要求水準の運用・thesis 手順のどこにも band 文脈を読む指示が無い。8/19 cycle の operator（AI）も参照せず 8.5 を素で適用した |
| 改訂 3 → starter band [7.0, 8.5) 導入（¥100k/注文・bucket 10%） | **適格 lane 0 件・starter 約定 0 件**。帯導入後の lane は base 5.35%（帯未満）と 11.39%（帯超・evidence gate で defer）のみ |
| — | **proposal 機構は累計 0 行**。直近の実買い（7/15 3836 @1,204）も reservation→execution の ledger 直接記録で、plan-limit → proposal → approve の last mile は production で一度も走っていない |

## 3. Funnel の崖は 2026-08-04 に立っている

shortlist ごとの selected（research 送り）件数:

```
7/17: 8   7/28: 8   7/29: 8   7/31: 6   8/3: 3   8/4: 0   8/6: 0   8/7: 0
8/10: 0   8/10b: 1   8/13: 1   8/14: 1   8/19: 1
```

8/4 は carry 残像対策（buyback 枠消化の annotation 化）と「直近 cycle で棄却済みの再登場は新材料なしに research 枠を再消費しない」規則の導入日で、供給集合がほぼ固定（8/19 の対前日 top-20 Jaccard 0.905 = 歴代 p100）の環境では、この規則が research 流量を cycle あたり 6〜8 → 0〜1 へ落とす（実行 lane 数では 7 月末の約 1.5 週で 11 lane → 8/7 以降の 2.5 週で 2 lane）。**規則自体は正しい**（同じ結論の再研究はコスト）が、starter band はこの流量低下の後に導入されたため、帯が捕捉対象とした分布（8/6 時点で 11 lane 中 3 lane が帯内）がもう流れてこない。帯が死んでいるのではなく、**帯へ水が来ていない**。

## 4. 反実仮想 — required は無効、倍率だけが決定的

保存済み thesis の scenario 入力（起点利益・株数・成長・株数変化・配当）を固定し、(a) 要求利回りを 8.5→8.0→7.5% に緩めた場合の FV、(b) 終端倍率を機械アンカー含意倍率へ置換した場合の base CAGR を engine と同式で再計算した。

| lane | base5 | 採用倍率 | 機械アンカー倍率 | base@機械倍率 | FV@8.5 | FV@7.5 | entry | 反転 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 6345 (7/29) | 3.90 | 13.68 | 12.5 | 2.36 | 1,143 | 1,197 | 1,420 | なし |
| 7943 (8/3) | 3.12 | 11.0 | 12.8 | 5.73 | 2,396 | 2,510 | 3,090 | なし |
| 6458 (8/4) | 6.91 | 11.5 | 12.3 | 8.16 | 1,149 | 1,203 | 1,237 | 倍率で帯内 |
| 6088 (8/4) | 7.49 | 11.5 | 13.6 | 10.54 | 554 | 580 | 580 | 倍率で 8.5 超 |
| 2121 (8/3) | 7.39 | 12.5 | 13.4 | 8.63 | 2,751 | 2,881 | 2,896 | 倍率で 8.5 超（ただし elevated） |
| 4887 (8/4) | 2.47 | 11.5 | **21.6** | 14.5 | 1,215 | 1,272 | 1,617 | **倍率反転は罠**（一過性 EPS がアンカーを汚染） |
| 6417 (8/17) | 5.35 | 10.78 | 10.0 | 4.01 | 1,666 | 1,745 | 1,931 | なし |
| 3836 (8/20) | 11.39 | 14.0 | 17.1 | 15.52 | 1,347 | 1,411 | 1,181 | もともと buy 水準 |

- **要求利回りの緩和（8.5→7.5%）で BUY へ反転する reject は 0 件**。FV と価格の乖離 15〜35% に対し、1pt の割引率変化は FV を 4〜5% しか動かさない。**閾値は binding でない。**
- 反転を生む唯一の仮定は**終端倍率**。ただし 4887 が示すとおり機械アンカーは一過性 EPS で汚染され得る（21.6x）。倍率は一律規則でなく、**検証済みの機構（6088 の粗利率 44.6→49.4% のような）を名指しできる場合に限り現観測より上を許す**判断領域である。
- machine E[r] → research base の haircut は中央値 **-4.2pt**（-8.0〜+2.1）。較正（§8）を重ねると、8.5% 要求の実効バーは「E[r]≥8.5% 帯の実現中央値」換算で約 20% 相当の higher bar として運用されている。

## 5. 高 base 非買い 4 lane の解剖 — 実現した機会費用の所在

| lane | base5 | 何に止められたか | その後 |
| --- | ---: | --- | ---: |
| 4432 (7/15) | 10.64 | **確信待ち defer**（「Q2 前に 8% 成長と 14 倍を同時に強く確信しない」。gate でなく判断） | **+21.1%** |
| 2121 (8/3) | 7.39 | **permanent_loss elevated**（structural_decline adverse・信用買い残 35.6 倍） | **+21.2%**（規律上は正しい見送り。結果は regime） |
| 6088 (7/29→8/4) | 9.88→7.49 | unknown 軸（1 人当たり粗利低下の判定不能）→ 再研究で base 切り下げ | **-5.1%**（defer が正しかった実例） |
| 3836 (8/20) | 11.39 | **evidence 鮮度 gate**（顧客集中が FY26 期末時点で未確認）→ 人間が override 不行使を選択 | +3.7%（当日） |

低 base の reject 群（2.4〜6.9%）の forward は **-3.1〜+1.8%（中央値 -0.9%）でほぼ横ばい** — 見送り自体はこの窓では損をしていない。実現した機会費用は「**base が帯内以上なのに、リターン以外の理由（確信・エビデンス・リスク軸）で二値の見送りに落ちた lane**」に集中しており、その半分（2121, 6088）は規律どおりの正解、残り（4432 型、3836 型）が**縮小 lot という中間手段があれば取れた**類型である。4432 は starter band 導入前、3836 は band の適用範囲外（base が帯の上限を超えており、evidence gap 型は band の条件に入っていない）。

## 6. Disposition 別 forward（shortlist 全 13 本・260 entries、publish as-of 終値 → 8/20）

| class | n | median | mean | min/max |
| --- | ---: | ---: | ---: | --- |
| selected（研究送り） | 37 | +1.2% | **+4.5%** | -9.9 / **+41.5** |
| rejected: one_off_earnings | 34 | +3.0% | **+6.1%** | -0.9 / +26.2 |
| rejected: price_already_converged | 85 | +2.1% | +2.7% | -3.9 / +33.1 |
| rejected: event_wait | 40 | +0.4% | +0.7% | -5.9 / +9.9 |
| rejected: その他 5 class | 28 | +1〜3% | +1〜4% | — |

上昇局面で全 class が薄く浮いている。**one_off_earnings 群の +6.1% は特別益 momentum の続伸**で、これを「捕るべきだった」と読むのは AP 系の失敗（一過性を実力視）に戻ることになる — 本 report はこれを機会損失に数えない。窓は 2〜7 週で重なり非独立、regime は上昇のみ（§10）。

## 7. 供給側の実勢（market-level の反証）

- 当日 selection top-5 平均 E[r] 11.27% は歴史 **p56（普通）**、panel の hurdle（8.5%）超え件数 5 件は **p16（薄い）** — 8/6 診断と同じ 2 座標の割れが継続。
- 幅は極端に狭い: 対前日 top-20 Jaccard 0.905（p100）、carry 支配 95%（p73）、event_wait 滞留 31.6%（p91）。
- つまり「割安が転がっているのに拾えていない」ではなく、**機械水準では普通の供給を、研究層の二重保守 × 流量 throttle × 二値の意思決定が 0 件へ落としている**、が正確な像である。

## 8. 較正の予実（機械 E[r]≥8.5% 帯、ticker-equal、price-only）

| horizon | 予測中央値 | 実現中央値 | cohort 数 |
| --- | ---: | ---: | ---: |
| 3y | 9.88% | **20.89%** | 35 |
| 5y | 10.07% | **17.18%** | 19 |

機械の最上位帯は予測の約 2 倍を実現してきた（2023〜2026 上昇局面を含む。regime 統制は同 as-of 母集団比のみ）。research base はこの機械値よりさらに中央値 -4.2pt 下にある。

## 9. 詰まりの再特定（8/6 診断の更新）

1. **改訂 2 の手順未実装が最大の未完**。band 実現中央値は comparison に焼き込まれているのに、research skill・OP3・required の運用のどこもそれを読まない。「8.5% を素で当てる運用から乖離を見た人間裁定へ」はまだ起きていない。
2. **starter band は条件でなく流量で死んでいる**。8/4 の再登場規則が（正しく）研究流量を絞った結果、帯内 base の新規 lane が発生しない。帯の捕捉対象を「base ≥ 帯下限 かつ 非 elevated かつ dated catalyst」の evidence-gap 型（3836 型・4432 型、base が帯上限超でも可）へ広げない限り、帯は今後も空転する。
3. **意思決定が二値**（full buy か見送り）。今回 3836 で機械は override + reduced の中間経路を提示できたが、これは手順化されておらず、人間に届いたのは本 study と同日が初。
4. **proposal last mile は未走行**。初回実走（3836 の 9/18 再評価が有力）は初回固有の欠陥を伴う前提で臨む。
5. 上流 E[r]・7 軸・elevated 不可・正規化原則は**変えない**（8/6 診断の改訂 1 を維持。#862 の holdout 反転も buy-side 緩和の設計規律として引き続き有効）。

## 10. 誠実性の限定

- forward 窓は 2〜7 週・重なり非独立・上昇 regime のみ。有意性・track record を主張しない。効果量の向きと集中箇所の特定にだけ使う。
- +21% × 2 は各 ¥250k 想定 lot に対する類型別の実現差であり、portfolio 全体の期待値差ではない。逆側の実例（6088 -5.1%）を同表に併記した。
- 反実仮想は保存済み scenario 入力の機械的再計算で、当時の判断者がその倍率を選べたかの反実仮想ではない（4887 の汚染例が示すとおり、選べない場合がある）。
- 較正実現値は上昇局面を含む。regime 統制された量は同 as-of 母集団比のみ（8/6 診断 §9 と同じ限定）。
- 現金機会費用は約 ¥3.0M × 5 週 × （reject 群中央値 +1〜2%）≈ 数万円規模の bounded な額であり、年率換算の演出はしない。

## 11. 監視事項

- 2026-10-17 の shortlist cohort 初回 3m 採点（8/6 診断 §10 から継続）。selected / rejected / machine の中央超過を band 別に記録する。
- 本 report の高 base 非買い 4 lane は個別に追跡する（4432・2121・6088・3836 の 3m/6m forward）。特に 2121（elevated での +21%）は「規律が正しくても結果が痛い」klass の代表として、elevated 判定の予実を積む。
- starter band の空転判定は「帯の条件拡張後、8 週間で starter 提案 0 件」を次の再点検 trigger とする。
