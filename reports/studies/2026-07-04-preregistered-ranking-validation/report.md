# 事前登録仮説 H1–H8 の design/confirm 検証とランキング改訂（WU4 #295）

計画 #291 §5 で事前登録した仮説を、design（cohort asof ≤ 2024-06-30）/ confirm（> 2024-06-30）の時間分割で検証し、通過した変更だけを select に反映する記録。計測基盤は PR #297/#298/#299（total return 基準・E[r] 導入済み store）。

## 0. 採否基準（検証実行前に固定・本節を先に commit）

**判定の一般則**: design と confirm の両方で同方向なら「支持」、片側のみ「不確定」、両側逆は「棄却」。有意性は主張しない（cohort 窓重複のため）。効果量と cohort 勝率のみで判定する。

**盲検性の限定（正直な位置づけ）**: 本基準の固定時点で、full-sample の er_annual IC（WU3 レポート）と現行推奨順の劣後（WU1 baseline）は既に公開・既知だった。したがって本検証の新規性は「design/confirm の時間分割でも方向・効果量が保つか」の頑健性確認であり、完全な事前盲検ではない。また design 末尾 cohort の forward 実現窓は confirm 前半と市場環境を共有する（forward 窓の bleed。12m ではより大きい）。

**H3（ランキング改訂 = 本 WU の実装対象）の採用 3 条件**（すべて design/confirm 両方で充足時のみ採用）:

1. **一次ゲート**: `er_annual` の mean rank IC > 0 かつ IC 正の cohort 率 ≥ 2/3（6m。12m は補助確認）
2. **二次確認（replay・6m mean median excess）**: `er_ranked_top10`（E[r] 順・screen 通過集合内）が (a) `recommended_rank_top5`（現行本番）を **+2pt 以上**上回り、(b) `selection_rank_top10`（現行順・diversity なし）以上
3. **トラップ非悪化**: `er_ranked_top10` の mean trap rate ≤ `recommended_rank_top5` の trap rate

**その他の仮説（H1/H2/H4–H8)**: 本 WU では判定を記録する。rule 変更（閾値・gate・条件の改廃）は候補集合が変わり panel 再生成を要するため、「支持」となった項目のみ別 issue で rules variant 計測を行う（本 PR では変更しない）。

**採用時の実装**: select の順位付けの主キーを playbook 固定順 → `er_annual` 降順（欠損は最後尾・従キーに現行 playbook 順 + strength key を残す）に変更する。diversity cap は sector cap を維持し、playbook cap は E[r] 主キー下での実測（er_ranked は cap なし計測のため、実装後の recommended_rank replay を再構築して確認）に基づき本 PR 内で最終決定する。

## 1. H3 判定（採用 — 3 条件すべて design/confirm 両方で充足）

| 条件 | design（22 cohort/6m） | confirm（18 cohort/6m） | 判定 |
| --- | --- | --- | --- |
| ① er_annual IC > 0・IC+ ≥ 2/3 | 0.226（IC+ 100%） | 0.148（94%） | ✓ |
| ② er_ranked_top10 − 現行推奨 top5 ≥ +2pt | +8.0% vs −6.7% = **+14.7pt** | +6.6% vs −7.2% = **+13.7pt** | ✓ |
| ②b er_ranked_top10 ≥ 現行順 top10 | +8.0% ≥ +1.8% | +6.6% ≥ +1.6% | ✓ |
| ③ trap 非悪化 | 12.3% ≤ 26.1% | 10.6% ≤ 29.2% | ✓ |

12m 補助: er_ranked_top10 は design +19.1% / confirm +13.9%（現行推奨 −3.5% / −1.1%）で同方向。issue #295 DoD の decile spread ゲート: er_annual の decile spread（best−worst median excess・6m）は design 平均 +18.1pt（正 22/22 cohort）/ confirm +13.6pt（15/18）。

**er_ranked の近似について**: er_ranked は pass_screen × er 非 null 集合の並べ替えで、本番 select の liquidity / sizing_eligible filter・上場廃止銘柄の枠縮小は再現しない近似。本番形の確認は §2 の panel 再構築後 recommended_rank replay が担う（両者は同方向・同水準）。

**diversity cap の変種比較**（E[r] 主キー・top-10・6m mean median excess）: playbook cap=2 は design +8.4% / confirm **+3.7%** と confirm で失速、cap 撤廃は **+7.6% / +7.6%** と両窓で頑健 → sector cap（2）は維持、playbook cap は実質無効化（10）を採用。cap=2 の飽和（推奨 8 銘柄で頭打ち）が baseline で測った害の主因。**この cap 選択は両窓の結果を見た post-hoc 判断であり、cap パラメータについて confirm の out-of-sample 性は消費済み**（H3 本体の 3 条件は margin が大きく影響しない）。採用構成（sector2 + playbook10）の本番形は §2 の表が正で、design 6m top-10 は +6.4% と cap 撤廃変種（+7.6%）よりやや低い。

**previous-candidate cap の整合**: 較正リプレイは previous_candidates を中立化して計測しており、本番の `max_previous_candidates_in_recommended: 2` は「E[r] 上位が月をまたいで持続する」正常な挙動と衝突して検証済み順位を運用で崩す。**計測した構成に本番を一致させるため null（cap なし）に変更**した（suppression = 人間の deferred/rejected 判断による除外は残る。これはリプレイが中立化した人間判断レイヤーで、乖離として本節に記録する）。

## 2. 採用した変更と本番形の前後比較

**変更**: (a) select の順位付け主キーを playbook 固定順 → **機械 E[r] 降順**（欠損は後置・従キーに playbook 順 + 強度キーを残す）、(b) `max_recommended_per_playbook` 2 → 10（sector cap 2 は維持）。records/_config/screening-rules/2026-07-04T000000+0900.yaml。

**本番 recommended（diversity 適用後）の前後**（mean median excess / 勝率 / trap）:

| | 旧 top-5 | **新 top-5** | 旧 top-10 | **新 top-10** |
| --- | --- | --- | --- | --- |
| design 6m | −6.7% / 27% / 26% | **+8.0% / 73% / 14%** | −4.4%※ / — / 22% | **+6.4% / 77% / 12%** |
| confirm 6m | −7.2% / 22% / 29% | **+3.2% / 56% / 18%** | — | **+9.5% / 72% / 11%** |
| design 12m | −3.5% / 36% / 29% | **+29.8% / 86% / 19%** | — | **+19.3% / 100% / 16%** |
| confirm 12m | −1.1% / 42% / 30% | **+11.4% / 75% / 19%** | — | **+13.0% / 67% / 18%** |

※旧 top-10 は playbook cap の飽和で実質 8 銘柄（baseline レポート参照）。新形は top-10 で平均 9.0–9.2 銘柄が埋まる。

## 3. その他の仮説の判定（rule 変更は本 PR に含めない）

| 仮説 | design → confirm | 判定 |
| --- | --- | --- |
| H1 益回り正 IC | ocf_yield 0.126→0.125・per_trailing 0.194→0.201（すべて正） | **支持** |
| H2 sector 相対 vs 自己レンジ | smg_pbr +0.156→+0.135 / srp_pbr −0.085→−0.104 | **支持**（自己レンジ percentile は両窓で負 — E[r] の自己中央値は cap 用途のみで整合） |
| H4 deterioration gate | 割安 decile 内の pass−block 差は小さく窓間で符号不安定 | 不確定（rule 変更なし） |
| H5 自社株買い | 0.174→0.105（IC+ 100%→83%・trap 最低クラス） | **支持**（E[r] carry に組込済み） |
| H6 イールドチェイス | dividend_yield D10 trap 5.5%→4.7%（母集団平均より低い） | **棄却**（この窓では利回り上位のトラップ上昇は確認されず。バリュー期レジーム注意） |
| H7 60 日下落条件 | IC 0.006→−0.046（6m）/ −0.012→+0.068（12m）と方向不一致。ただし D10 trap 26–38% は全軸最悪で両窓一致 | 不確定（「深押し追いは高トラップ」のみ一貫。条件 B の rule 変更は rules variant 計測を要するため別 issue） |
| H8 per_forward > per_trailing | 0.213 vs 0.194 → 0.207 vs 0.201（12m も同方向） | **支持**（差は小・E[r] の収益 anchor は forward 優先で整合） |

診断（変更なしの記録）: `er_population_top10`（screen gate なしの母集団 E[r] 選抜）は design +14.1% / confirm +6.7% と er_ranked（gate 内）以上 — **playbook screen gate は E[r] の上では選抜価値を足していない**。universe gating の再設計は本プログラム外の将来課題として記録する。

## 3.5 運用テスト（現 asof 2026-07-01 の select 前後）

- 旧順（playbook 主キー）top-5: 5949, 4839, 6619, 2168, 2154（先頭の E[r] 4.3%/0.4%/**0.0%**…）
- 新順（E[r] 主キー）top-5: 4116（E[r] 18.4%）, 4008（14.9%）, 2168（8.5%）, 4559（8.1%）, 3825（5.4%）
- E[r]=0 の value-trap 形（無配 + 希薄化の 6619）が top-5 から外れ、降順が成立していることを確認。

**監視事項**: confirm 窓で新 top-5（+3.2%）< 新 top-10（+9.5%）の逆転がある — E[r] 極値の最上位が中位より弱い（deep discount の実現減衰と同根の可能性）。月次の再計測で top-5 の劣化を追い、持続するなら E[r] 上位の quality 併用条件を #291 後続で検討する。

## 4. 検証・再現

```bash
uv run baibai-loop-screening calibration-build --start 2022-09-01 --end 2026-03-31 --force
uv run baibai-loop-screening calibration-evaluate --horizon 6m --start 2022-09-01 --end 2024-06-30 --out .cache/wu4-design-6m.yaml
uv run baibai-loop-screening calibration-evaluate --horizon 6m --start 2024-07-01 --end 2026-03-31 --out .cache/wu4-confirm-6m.yaml
```

- 事前登録の順序は git history が正本（§0 の commit → 計測 → 本節の追記）。
- 統計の誠実性: 有意性は主張しない。効果量・cohort 勝率・trap 率のみで判定（cohort 窓は重複）。
- レジーム注意: 計測窓全体がバリュー優位。E[r] 順位の優位が他レジームでも保つかは、毎月増える cohort と前提検証（refresh 的な再計測）で追う。
