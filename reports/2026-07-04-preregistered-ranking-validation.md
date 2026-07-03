# 事前登録仮説 H1–H8 の design/confirm 検証とランキング改訂（WU4 #295）

計画 #291 §5 で事前登録した仮説を、design（cohort asof ≤ 2024-06-30）/ confirm（> 2024-06-30）の時間分割で検証し、通過した変更だけを select に反映する記録。計測基盤は PR #297/#298/#299（total return 基準・E[r] 導入済み store）。

## 0. 採否基準（検証実行前に固定・本節を先に commit）

**判定の一般則**: design と confirm の両方で同方向なら「支持」、片側のみ「不確定」、両側逆は「棄却」。有意性は主張しない（cohort 窓重複のため）。効果量と cohort 勝率のみで判定する。

**H3（ランキング改訂 = 本 WU の実装対象）の採用 3 条件**（すべて design/confirm 両方で充足時のみ採用）:

1. **一次ゲート**: `er_annual` の mean rank IC > 0 かつ IC 正の cohort 率 ≥ 2/3（6m。12m は補助確認）
2. **二次確認（replay・6m mean median excess）**: `er_ranked_top10`（E[r] 順・screen 通過集合内）が (a) `recommended_rank_top5`（現行本番）を **+2pt 以上**上回り、(b) `selection_rank_top10`（現行順・diversity なし）以上
3. **トラップ非悪化**: `er_ranked_top10` の mean trap rate ≤ `recommended_rank_top5` の trap rate

**その他の仮説（H1/H2/H4–H8)**: 本 WU では判定を記録する。rule 変更（閾値・gate・条件の改廃）は候補集合が変わり panel 再生成を要するため、「支持」となった項目のみ別 issue で rules variant 計測を行う（本 PR では変更しない）。

**採用時の実装**: select の順位付けの主キーを playbook 固定順 → `er_annual` 降順（欠損は最後尾・従キーに現行 playbook 順 + strength key を残す）に変更する。diversity cap は sector cap を維持し、playbook cap は E[r] 主キー下での実測（er_ranked は cap なし計測のため、実装後の recommended_rank replay を再構築して確認）に基づき本 PR 内で最終決定する。

（以下の節は基準 commit 後に計測値を記入）
