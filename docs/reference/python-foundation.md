---
title: "Python foundation"
summary: "repository固有の開発環境、validationとtestの分担、完全local gate。"
doc_type: reference
status: active
source_paths:
  - "../../engine/src/baibai_engine/"
  - "../../web/backend/src/baibai_web/"
  - "../../batch/src/baibai_batch/"
  - "../../tests/"
  - "../../pyproject.toml"
  - "../../uv.lock"
---

# Python foundation

## 1. Runtime policy

対応環境は[architecture](../architecture.md)に従う。Pythonの許容範囲は`pyproject.toml`、実行patchは`.python-version`、Node.jsはWeb workflowを正本とする。

## 2. Dependency policy

依存は`pyproject.toml`と`uv.lock`で管理する。開発環境は`uv sync --frozen --all-groups`で揃える。MCP SDKはOwner MCPとTradingView collectorで共用するruntime依存とする。

## 3. Formatting and linting

Ruffを使う。runtimeはPython 3.14だが、formatter targetは`py313`である。複数例外捕捉を`except (T1, T2):`に揃え、PEP 758の括弧なし構文へ自動変換させないための選択である。

Markdownのhard breakを壊さないよう、trailing whitespace hookは`.md`を対象外とする。その他の設定値は`pyproject.toml`とpre-commit設定を参照する。

## 4. Type checking

CIの型検査はmypy strictに統一し、runtime packageとtoolsを対象にする。IDEの型検査は開発者の選択とし、別のCI type gateを追加しない。

## 5. Validation boundary

外部入力と保存境界をdomain model・DB constraint・serviceで検証する。providerの正規化、文書内部の整合、現在のsourceとの整合はそれぞれのownerが担う。内部計算に同じvalidationを重ねない。過去の保存物の読取と、新しい判断の適格性を区別する。

## 6. I/O and adapters

外部I/Oはadapter、domain処理はengineへ置く。保存・原子的な書込・YAML読込は既存の共通処理を使う。CLI・batch・Webがdomainの算術や保存規則を再実装しない。

## 7. Testing and coverage

domainの契約はownerのtestで確認し、CLI・batch・Webは自分の入出力と接続を検証する。同じ条件を全層で再列挙しない一方、assert本数やcase数に固定の上限は置かない。

fixtureの共通化は意味が同じ場合に行う。builderでproductionにない制約を作らない。testの削除・統合は、重複している契約と残る検出力から判断する。

coverageはpytest-covで計測する。worker数と閾値は設定・workflowを正本とし、性能変更は同じ入力・環境で比較する。過去の所要時間を現在の性能保証として常設しない。

## 8. Security checks

sourceの静的検査はBandit、依存監査はruntime/devについてlock由来のpip-audit、build-systemについて`[build-system].requires`由来のpip-audit、およびnpm auditを使う。実行環境にたまたま入ったpackageをproject依存として監査しない。advisoryはcodeが変わらなくても更新されるため、依存監査には週次実行も持つ。

CodeQLは現行構成では採用しない。scanner追加は既存検査との差分と運用価値で判断する。workflowの権限・Action固定・dispatch入力は既存trust gateが検証する。

## 9. CI and local parity

各blockはrepository rootから独立して実行し、非zeroなら原因を直してから次へ進む。実際のtrigger/stepは`.github/workflows/`を参照する。

```bash
(
  set -e
  uv sync --frozen --all-groups
  uv run ruff format --check .
  uv run ruff check .
  uv run mypy
  uv run lint-imports
  for gate in tools/quality/drift/check_*.py; do
    uv run python -m "tools.quality.drift.$(basename "$gate" .py)"
  done
  TMPDIR=/dev/shm uv run pytest -n 4 --cov --cov-report=term-missing
  uv run bandit -c pyproject.toml -q -r engine/src/baibai_engine web/backend/src/baibai_web batch/src/baibai_batch tools
  uv export --format requirements.txt --locked --all-groups --no-emit-project --no-hashes --output-file /tmp/baibai-loop-requirements.txt
  uv run pip-audit -r /tmp/baibai-loop-requirements.txt
  uv run python - <<'PY' > /tmp/baibai-loop-build-requirements.txt
import tomllib
from pathlib import Path

for requirement in tomllib.loads(Path("pyproject.toml").read_text())["build-system"]["requires"]:
    print(requirement)
PY
  uv run pip-audit -r /tmp/baibai-loop-build-requirements.txt
)
```

Node依存監査:

```bash
(
  set -e
  (cd web/frontend && npm audit --package-lock-only --audit-level=high)
  (cd web/edge && npm audit --package-lock-only --audit-level=high)
)
```

Web UI と Worker:

```bash
(
  set -e
  (cd web/frontend && npm ci && npm run lint && npm run build && npm test)
  (cd web/edge && npm ci && npm run types:check && npm run typecheck && npm test && npx wrangler deploy --dry-run --outdir /tmp/baibai-worker-bundle)
)
```

macroの計算・取得・registry・reading methodを変更した場合は、localの対象storeも検証する。

```bash
uv run baibai-batch validate-macro-stores
```

storeがなければ未実行と報告する。CIにはapplication storeがなく、通常gateは代用にならない。文書だけの変更でlive取得・本番再発行を行わない。

## 10. Review rule

設定・workflow・本節のcommandを整合させる。通常の開発変更に別の承認手順、検査結果台帳、全domainの再検証を追加しない。
