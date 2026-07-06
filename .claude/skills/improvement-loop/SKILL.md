---
name: improvement-loop
description: >-
  Baibai-Loop のトレード基盤（マクロ読み・screening 選定・E[r]/FV/RR 見積り）の精度を
  計測で改善するサイクルを回す操作手順。現状計測 → 仮説の事前登録 → design/confirm 検証 →
  採用実装 → 運用テスト → dated report → 月次監視まで。「基盤を改善して」「改善ループを回して」
  「calibration で検証して」「screening / E[r] の精度を上げて」「rules variant を計測して」
  と言われたとき、および select ランキング・screen 閾値・E[r] パラメータの改訂を伴う
  すべての変更で使う。
---

# 改善ループ（Baibai-Loop）

正本は [`docs/operations/improvement-loop.md`](../../../docs/operations/improvement-loop.md)（サイクル定義・誠実性規律・改善対象マップ）と [`docs/reference/estimate-calibration.md`](../../../docs/reference/estimate-calibration.md)（計測仕様）。本 skill は操作の順序と落とし穴だけを持つ。

## 鉄則

1. **採否基準を計測の前に commit する**（事前登録）。基準を後から動かした時点でその検証は無効。
2. **design/confirm の時間分割両方で通った変更だけ採用する**。有意性は主張しない（効果量 + cohort 勝率）。
3. **計測した構成と本番構成を一致させる**。計測で中立化したレイヤー（cap・suppression）が本番だけに残ると検証済み順位が崩れる。
4. **1 改善 = 1 issue = 1 PR**。レビュー反映・運用テストのバグ修正は同 PR にコミットを積む。

## 手順

### 1. 現状計測と仮説

```bash
cd /home/kou/baibai-loop
uv run baibai-loop-screening calibration-build --start 2022-09-01 --end <直近の完全月末>   # rules 改訂後は --force
uv run baibai-loop-screening calibration-evaluate --out .cache/calibration-eval-current.yaml
```

- 直近の `reports/*estimate-calibration*` / `*validation*` の監視事項と突き合わせ、改善仮説を issue 化する（観察 → 仮説 → 検証方法 → 着手条件）。
- 改善レバーの所在は正本 doc の「改善対象マップ」を引く。

### 2. 事前登録

- report（`reports/YYYY-MM-DD-<slug>.md`）の冒頭に採否基準（数値）を書いて **先に commit** する。
- 既知の結果がある場合は盲検性の限定を正直に書く。

### 3. design/confirm 検証

```bash
# rules variant は本番 rules を触らず、別 store に panel を構築する
uv run baibai-loop-screening calibration-build --rules-path <variant rules yaml> \
  --start 2022-09-01 --end <直近の完全月末> --calibration-dir data/screening/calibration-<variant>
uv run baibai-loop-screening calibration-evaluate --horizon 6m --start 2022-09-01 --end 2024-06-30 \
  --calibration-dir data/screening/calibration-<variant> --out .cache/<variant>-design-6m.yaml
uv run baibai-loop-screening calibration-evaluate --horizon 6m --start 2024-07-01 --end <直近> \
  --calibration-dir data/screening/calibration-<variant> --out .cache/<variant>-confirm-6m.yaml
```

- 判定: 両窓同方向 + 基準充足 = 採用 / 片側のみ = 不確定 / 両側逆 = 棄却。12m は補助確認。

### 4. 採用実装 + 運用テスト

- 通過した変更だけ本番（`records/_config/screening-rules/` / `src/baibai_loop/screening/`）へ反映し、panel を `--force` 再構築して本番形 replay の前後比較を確認する。
- 現 asof で `run` → `select` を回し、上位の実銘柄で意図した挙動を確認する（E[r] 降順の成立・value-trap 形の脱落・異常値の有無）。

### 5. 記録とマージ

- dated report に再現手順・coverage / survivorship 開示・判定表・検算（AP-02）・**採用後の監視事項**を固定する。
- マージ前ゲート: `uv run baibai-loop-validation` / `ruff format --check .` / `ruff check .` / `mypy` / `pytest` + 運用テスト。
- マージ後は月次サイクル（[`docs/operations/monthly-cycle.md`](../../../docs/operations/monthly-cycle.md) §8）で監視事項を追う。

## 落とし穴

- `get`/`evaluate` 系の出力は「cache にあるもの」を返す。rules を変えたのに `--force` を忘れると旧 panel を測り続ける（rules_hash guard が検出して fail するのが正常）。
- cohort 窓は重複していて独立でない。「n が大きいから有意」という言い方をしない。
- 計測窓のレジーム（バリュー優位等）を結論に併記する。単一レジームの窓で普遍を主張しない。
- 深い調査はメインコンテキストで行い、サブエージェント fan-out は最大 5・read-only 探索に限る。
