# Second-run checks

同じ operation を再度なぞるときの create-only、hash、CAS の境界を実コマンドで確認する。

| check | result | evidence |
| --- | --- | --- |
| snapshot output overwrite | expected reject | exit 1: `refusing to overwrite existing output: .../evidence-snapshot.json` |
| blind-freeze output overwrite | expected reject | exit 1: `refusing to overwrite existing blind freeze: .../blind-freeze.json` |
| report output overwrite | expected reject | exit 1: `refusing to overwrite existing output: .../one-page.md` |
| indicator scaffold draft path | overwrite confirmed | 同じ既存 `--output` への 2 回目は exit 0 で `wrote 90 indicator inputs`。CLI 自体は上書きを拒否しない。cycle directory の存在 guard がこの path へ到達する前に停止させる。 |
| snapshot hash, same stable inputs | pass | 別の一時出力へ連続 2 回生成し、いずれも `942d92637ec32bdff87d891e4996aa69ebaec35c706c3d694b9c6b11d1579adf`。 |
| snapshot hash vs frozen artifact | pass | 上記 2 回の canonical hash は、freeze 対象として保存済みの `evidence-snapshot.json` の hash と一致する。 |
| stale expected-head CAS | expected reject, no mutation | exit 1: `macro context head changed: expected='macro-context-2026-08-07-labor-capex-divergence', actual='macro-context-2026-08-12-yen-retracement-real-rate-squeeze'`。拒否後の head は新 context のまま。 |
| freeze integrity after v4 edits | pass | v4 draft の修正と revision diff の追記を挟んだあと、`check --freeze --v4-projection` が `e33acb7e4208139044795376a0f2c3a4997ba30c73adaa6deabb58eeadcc1bb7` で ok を返す。 |

## cycle 1 で残った問いへの回答

cycle 1 の second-run check は、初回 freeze 後の hash と再実行 hash が一致しなかった原因を「非決定性ではなく、
scorecard settle 証明のために 8 series を明示 refresh して L1 の入力集合が増えたこと」と記録し、
cycle 2 では scorecard proof に使う series を blind snapshot 前に取得できるかを検討するとした。

cycle 2 ではこの問い自体が発生しなかった。前回 head の scorecard は 6 条件で met 1 / not_met 0 / pending 5 であり、
唯一の `met`（us.sox ≥ 11,000）は日次バッチの再取得窓が既に評価窓を覆っていたため、run 証明の不足による
error が出ず `macro refresh` を 1 回も実行していない。結果として **freeze 前後で L1 store が不変**となり、
full operation を先頭から再実行した canonical hash が凍結済み artifact と完全に一致した。

したがって「同一入力に対する冪等性」は cycle 1 と同じく成立し、加えて cycle 2 は
**「full operation 自体が L1 を変えない場合がある」**ことを実測で示した。refresh が要るかどうかは
前回 scorecard の met / not_met の構成に依存するため、pre-freeze の proof refresh を tooling 化する必要性は
毎 cycle 一定ではない。deferred のまま据え置き、promotion 判定時に採否を再評価する材料としてここに残す。
