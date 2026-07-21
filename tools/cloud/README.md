# tools/cloud — クラウド配信向けのローカルバッチ script

Cloudflare 配信（issue #467 の設計）の compute 側入口。いずれも将来 GitHub Actions
の workflow がそのまま呼ぶ前提の script で、stable CLI ではない（安定契約は
`baibai-engine` / `baibai-app` 側にあり、この 2 script は orchestration のみを持つ）。
ローカル単体でも完結して動き、クラウド資源を一切必要としない。

## export_read_models.py — read model の材料化

`baibai_app.readmodel` builders を共用して、UI が読む全 view を serving 配置どおりの
JSON に書き出す。Worker には業務ロジックを置かない設計の実体。

```bash
uv run python tools/cloud/export_read_models.py --output-dir <dir> [--batch daily|manual] [--repo-root <path>]
```

出力（`<dir>` 配下）:

- `views/dashboard.json` / `views/screening_latest.json` / `views/program.json`
- `views/macro--<period>-<granularity>.json`（1y|5y|10y|max × daily|weekly|monthly|yearly）
- `views/security--<ticker>.json`（保有 + 最新 run 掲載 + reviewed shortlist の ticker）
- `views/meta.json`（生成時刻・store 別 as-of・batch 種別。UI の鮮度表示と同じ契約）
- `history/select/<asof>.json`（machine selection のサマリ。無期限保持する軽量履歴）
- `history/candidate-pool/<asof>.json`（candidate pool 全件の機械出力。31 日で削除する履歴）

views の JSON は `baibai-app` の対応 API response と同形（pydantic `model_dump_json`）。

## daily_batch.py — 日次機械工程の 1 コマンド実行

営業日判定 → screening cache coverage（不足時のみ bootstrap）→ run → select →
macro series refresh + import-manual → export → run store prune を順に実行する。
全 step は public CLI の subprocess で、step ごとにコマンドライン・exit code・所要秒を
stdout へ出す（scheduled workflow のログをそのまま読む前提）。

```bash
# 通常（当日 JST。market calendar で非営業日なら exit 0 で skip）
uv run python tools/cloud/daily_batch.py --output-dir <dir>

# 手動再実行・過去日（営業日 gate を skip）
uv run python tools/cloud/daily_batch.py --asof YYYY-MM-DD --output-dir <dir>
```

終了コード:

| exit | 意味 |
| --- | --- |
| 0 | 完走。または非営業日（当日 gate で `skip` を出して即終了） |
| 1 | 致命的失敗で停止（screening chain・営業日判定・calendar 不備。publish に至らない） |
| 3 | export まで publish 済みだが、繰延べステップ（macro refresh / import-manual / prune）が失敗 |

失敗ポリシー:

- screening 系（coverage / run / select）の失敗は致命的で即停止する（publish できる新しい
  run が無いため exit 1）。ただし `screening run` の exit 2 は品質警告つきの published run で
  あり、警告理由を表示して続行する。`verify-cache-coverage` の exit 1 は cache 不足マーカーが
  ある場合だけ bootstrap へ進み、マーカー無しの exit 1（rules 破損等の crash）は即停止する
- macro series refresh の失敗は繰延べる: export まで完走して screening 結果は publish し、
  最後に exit 3 で終了する（scheduled workflow の失敗通知は発火し、鮮度は meta の
  `macro_asof` に現れる）。繰延べた失敗の詳細は発生時点で stderr にも出す
- `select` の前回 run 比較は、runs store の「target より前の最大 as-of の最新 revision」を
  この script が決定論的に解決して `--previous-run-revision-id` で渡す（同一日の再実行が
  複数 revision を作っても停止しない）
- 営業日判定は market store の `jquants_market_calendar` が情報源。対象日をカバーして
  いない場合は黙って続行せず明示エラーで停止する
