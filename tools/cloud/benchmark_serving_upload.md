# serving upload concurrency benchmark

`benchmark_serving_upload.py`は、productionと同じ`views`同期 → history追記 →
`views/meta.json`最終uploadを、Workerから到達しない
`benchmarks/serving-upload/<run-id>/`だけで比較する。production keyや
`r2_transfer.sh`の並列度は変更しない。

固定したbefore / after exportを、credentialを使わない`plan`で先に検査する。入力は
current userが所有して他userから書けないregular file / directoryだけを受理し、
`views/`と`history/candidate-views/`以外、symlink、`views/meta.json`欠落、prefixを
逸脱できるrun IDを拒否する。run IDは他の計測と重ならないsuffixを付ける。

```bash
uv run python tools/cloud/benchmark_serving_upload.py plan \
  --before-dir /tmp/serving-before \
  --after-dir /tmp/serving-after \
  --run-id 20260731-serving-a1 \
  --output /tmp/serving-upload-plan.json
```

クラウド計測時の`run`はreview済みplanのdigestと、raceを拒否しながらprivate directoryへ
固定したexportが完全一致する場合だけR2へ進む。concurrency 10 / 20 / 40を順序を
入れ替えて各5回実行し、毎試行を固定concurrencyで同じbeforeへreset・hash検証してから、
mtimeを正規化した同じchanged / added objectを転送する。

各試行後に全objectのkey・size・SHA-256を検証する。最後にrun IDのprefixだけを削除して
空であることを確認する。開始時はowner tokenとplan digestを持つclaim objectを
`If-None-Match: *`で作成し、同じrun IDの同時実行を一方だけに限定する。reset、検証、
cleanupはclaim一致を要求する。local claim fileもexclusive createして同じpathの上書きを
拒否する。既にobjectがあるrun IDは削除せず拒否する。ローカル`.env`の
stores限定principalは使わず、`baibai-serving`だけをread-writeできるcredentialを明示的に
環境へ渡す。script側はその権限をbenchmark prefix以外に使わない。

```bash
uv run python tools/cloud/benchmark_serving_upload.py run \
  --before-dir /tmp/serving-before \
  --after-dir /tmp/serving-after \
  --run-id 20260731-serving-a1 \
  --plan /tmp/serving-upload-plan.json \
  --claim-file /tmp/serving-upload-claim.json \
  --output /tmp/serving-upload-report.json
```

reportはreview済みplan digest、upload contract version、AWS CLI version、実行環境、
15試行、各armのp50、planned workload digest、reset / 最終manifest検証、cleanup結果を
保持する。concurrency 10比でp50が30%以上短縮する最小armだけを推奨するが、production設定は
変更しない。productionと同種のrunnerで得たreportをreviewし、設定変更は別の変更として行う。

claim fileは中断時の所有権確認に使うため保持する。手動cleanupはclaimとrun IDが一致する
prefix以外を削除しない。

```bash
uv run python tools/cloud/benchmark_serving_upload.py cleanup \
  --run-id 20260731-serving-a1 \
  --claim-file /tmp/serving-upload-claim.json
```
