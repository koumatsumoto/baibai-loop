# 経済確認時計の有限read-only実行例

実行条件・判定と回復の境界は[OPERATIONS](../../jobs/OPERATIONS.md#経済原本)に従う。以下は初回のnikkei-pbr限定手順であり、本番で実行済みという記録ではない。#1962/#1965の統合・対象SHA/CI確認後、EDINET移送と競合しない認可環境で実行する。通常取得・公開・lease・writer設定には触れない。

## 1. 明示stageのmetadataを固定する

STAGE_IDは実際のcloud-economic-sourceのhandoff出力から選ぶ。stage名のasofは受領時計ではない。初回は最大10stageとし、integrated済みも含める。全prefix走査はしない。単一stageなら例の第二ID引数を除き、追加stageはID引数を増やす。以下のmetadataだけでは、対象観測日や受領窓の網羅は証明できない。fixtureの2026-11-06・18/20/21時を本番scopeへコピーしない。

既存ownerの入力はstage_prefix(ID)配下のhandoff.json、出力は前後HEAD/ETagとSHAを検証したbytes。markerは元manifest/source/started_atとの対応をこの有限手順で検査する。credentialを引数や出力へ渡さず、既存の認可済み環境変数を使う。

```bash
umask 077
set -o noclobber
uv run python - "$STAGE_ID_1" "$STAGE_ID_2" > "$FIXED_METADATA_JSON" <<'PY'
import hashlib
import json
import os
import sys

from baibai_engine.storage.objects.json import read_private
from baibai_engine.storage.objects.r2 import Boto3R2Store
from baibai_jobs.acquisition.economic_staging import stage_prefix

store = None
try:
    identities = sys.argv[1:]
    if not 1 <= len(identities) <= 10 or len(set(identities)) != len(identities):
        raise ValueError("select one to ten distinct stages")
    if any(not identity.startswith("nikkei_indexes-") for identity in identities):
        raise ValueError("select only Nikkei stages")
    store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
    targets, markers = [], {}
    for identity in identities:
        saved = read_private(store, stage_prefix(identity) + "/handoff.json")
        if saved is None:
            raise ValueError("selected manifest is missing")
        result = json.loads(saved.payload)["result"]
        status = next(row["status"] for row in result["sources"] if row["source_id"] == "nikkei-pbr")
        if result["group"] != "nikkei_indexes" or status not in {"ok", "failed"}:
            raise ValueError("selected source differs")
        digest = hashlib.sha256(saved.payload).hexdigest()
        targets.append({"group": "nikkei_indexes", "stage_id": identity,
                        "manifest_sha256": digest, "source_id": "nikkei-pbr",
                        "acquisition_status": status})
        marker = read_private(store, stage_prefix(identity) + "/integrated.json")
        if marker is None:
            markers[identity] = {"present": False, "status": "absent"}
        else:
            value = json.loads(marker.payload)
            if (value["manifest_sha256"] != digest
                    or value["source"] != "economic-nikkei_indexes"
                    or value["started_at"] != result["started_at"]
                    or value["status"] not in {"integrated", "needs_explicit_recovery"}):
                raise ValueError("marker differs from the fixed original handoff")
            markers[identity] = {"present": True, "status": value["status"],
                                 "sha256": hashlib.sha256(marker.payload).hexdigest()}
    print(json.dumps({"targets": targets, "markers": markers, "coverage": "not_established"}))
except Exception as error:
    print(json.dumps({"status": "failed", "reason": type(error).__name__}))
    raise SystemExit(1) from None
finally:
    if store is not None:
        store.close()
PY
```

status=failedなら次へ進まない。metadataに原本本文やproviderエラー文を出さない。available_handoffsを使う別の確認では、source="economic-nikkei_indexes", market=False, include_integrated=Trueが必要。ただしこのAPIはgroup全体のmanifestを読むため、上の有限手順の代わりに無制限に呼ばない。

## 2. 固定stageとcanonicalを私有copyへ取得する

OUTは未作成の私有directoryを指定する。同じfileへ再試行して上書きせず、失敗copyは残す。既存のGuardedInputStoreでput_fileを拒否し、read_private・download_fixed・_readに指定stageとindicators.sqliteだけを渡す。撤去済み診断やworkflowは呼ばず、旧schema移行や回復copy作成も行わない。

download_fixedへの入力はstage_id/market=False/manifest_sha256。出力はreceipts.sqliteとresult.jsonだけなので、元handoff.json・integrated.jsonはread_privateで別に保存する。markerがmetadata固定後に変わったら止める。_readはcanonicalの前後ETag/size/schema/confirmationを検査し、最後に全読取objectの不変を再確認する。

```bash
umask 077
uv run python - "$OUT" "$FIXED_METADATA_JSON" <<'PY'
import hashlib
import json
import os
import sys
import re
from pathlib import Path

from baibai_engine.storage.objects.json import read_private
from baibai_engine.storage.objects.r2 import Boto3R2Store
from baibai_engine.storage.snapshots.sources import sha256_file
from baibai_jobs.acquisition.economic_staging import stage_prefix
from baibai_jobs.acquisition.receipts import download_fixed
from baibai_jobs.integration.economic_handoff import validate_handoff
from baibai_jobs.integration.economic_sources import _read
from baibai_jobs.acquisition.source_inputs import GuardedInputStore

store = None
try:
    output, metadata = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text())
    targets = metadata["targets"]
    if not 1 <= len(targets) <= 10 or len({t["stage_id"] for t in targets}) != len(targets):
        raise ValueError("select one to ten distinct stages")
    if any(t["group"] != "nikkei_indexes" or t["source_id"] != "nikkei-pbr"
           or not t["stage_id"].startswith("nikkei_indexes-") for t in targets):
        raise ValueError("select only Nikkei PBR")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
    def reject_publish():
        raise PermissionError("read-only copy cannot publish")

    reader = GuardedInputStore(store, reject_publish)
    canonical = output / "canonical.sqlite"
    remote = _read(reader, canonical)
    digest = sha256_file(canonical)
    if remote.metadata.get("sha256") not in {None, digest}:
        raise ValueError("canonical content differs from metadata")
    observed = {"indicators.sqlite": remote}
    for target in targets:
        identity = target["stage_id"]
        prefix = stage_prefix(identity)
        packet = output / identity
        saved = read_private(reader, prefix + "/handoff.json")
        if saved is None or hashlib.sha256(saved.payload).hexdigest() != target["manifest_sha256"]:
            raise ValueError("selected manifest changed")
        manifest = json.loads(saved.payload)
        # Validate the finite key before HEAD, then let the owner verify bytes/schema.
        if re.fullmatch(r"[0-9a-f]{64}", manifest["receipt_sha256"]) is None:
            raise ValueError("invalid receipt digest")
        receipt_key = prefix + "/receipts-" + manifest["receipt_sha256"] + ".sqlite"
        if manifest["receipt_key"] != receipt_key:
            raise ValueError("receipt is outside the selected stage")
        for key in (prefix + "/handoff.json", prefix + "/integrated.json", receipt_key):
            observed[key] = reader.head(key)
        entry = {"stage_id": identity, "market": False, "manifest_sha256": target["manifest_sha256"]}
        download_fixed(reader, entry, packet)
        validate_handoff(packet, target["group"])
        for name in ("handoff.json", "integrated.json"):
            saved = read_private(reader, prefix + "/" + name)
            if name == "integrated.json":
                current = hashlib.sha256(saved.payload).hexdigest() if saved else None
                if current != metadata["markers"][identity].get("sha256"):
                    raise ValueError("marker changed after metadata selection")
            if saved is not None:
                (packet / name).write_bytes(saved.payload)
    for key, expected in observed.items():
        if reader.head(key) != expected:
            raise ValueError("selected object changed during copy")
    receipt = {"canonical_sha256": digest, "canonical_etag": remote.etag, "targets": metadata["targets"]}
    (output / "copy.json").write_text(json.dumps(receipt))
    print(json.dumps({"status": "copied_read_only", "canonical_sha256": digest, "stages": len(targets)}))
except Exception as error:
    print(json.dumps({"status": "failed", "reason": type(error).__name__}))
    raise SystemExit(1) from None
finally:
    if store is not None:
        store.close()
PY
```

copy.jsonはこの取得時点の私有控えであり、公開・回復の許可でも永続台帳でもない。raw/metadataを外部へuploadしない。取得後も現在headは進み得るため、結果は固定copy時点の調査として扱う。

## 3. local診断へ渡す

[既存CLI](../../jobs/OPERATIONS.md#経済原本)へ、copy.jsonのcanonical SHA、各targetのstage ID/manifest SHA、OUT/STAGE_ID、実原本で確認して固定したsource/day・受領時計窓を渡す。各stageにつき--stageを一つ渡す。手順1/2では原本行から対象日を自動選択しない。次のコマンドはlocalだけを読み、report.jsonを新規作成する。

```bash
umask 077
uv run python - "$OUT" "$OBSERVED_DAY" "$RECEIVED_SINCE" "$RECEIVED_UNTIL" <<'PY'
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

from tools.diagnostics.inspect_economic_confirmation_gaps import main

output = Path(sys.argv[1])
receipt = json.loads((output / "copy.json").read_text())
args = ["--canonical", str(output / "canonical.sqlite"),
        "--canonical-sha256", receipt["canonical_sha256"],
        "--source-id", "nikkei-pbr", "--start", sys.argv[2], "--end", sys.argv[2],
        "--received-since", sys.argv[3], "--received-until", sys.argv[4]]
for target in receipt["targets"]:
    args += ["--stage", target["stage_id"], target["manifest_sha256"],
             str(output / target["stage_id"])]
with (output / "report.json").open("x", encoding="utf-8") as report, redirect_stdout(report):
    code = main(args)
raise SystemExit(code)
PY
```

read-only準備とCLI接続は[test](../../tests/tools/test_economic_confirmation_readonly_recipe.py)のMemoryR2Storeで検証する。成功しても、過去の全影響・全stage網羅・保存済みL2への影響は未確認。異常時は人が対象・証拠を確認し、この手順に再取得・accept/merge・本番公開を足さない。
