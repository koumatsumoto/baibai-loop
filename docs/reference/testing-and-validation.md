---
title: "Testing and validation reference"
summary: "records の schema・validator の責務境界・テスト・ローカル検証コマンドの参照情報。"
doc_type: reference
status: active
last_reviewed: 2026-07-12
source_paths:
  - "../../records/_schemas/"
  - "../../src/baibai_loop/validation/"
  - "../../tests/"
---

# Testing and validation

`records/_schemas/` は records 成果物の形を固定する支援領域。validator は、schema だけでは表現しにくいファイル横断のルールと参照先の実在確認を補完する。

YAML front matter の `ticker` は必ず quote する（`"9715"`）。unquoted は int として parse され、schema の `type: string` 違反で validator が error にする（先頭 0 落ちや `130A` のような英字入り code の破損防止）。

## Boundaries

| area | 責務 |
| --- | --- |
| `records/_schemas/` | YAML / front matter の schema 正本 |
| `src/baibai_loop/validation/` | schema validation と cross-file validation |
| `tests/test_validate_*.py` | validator の期待挙動 |
| `.github/workflows/ci.yml` | PR / main push の local parity gate |
| `tools/drift/` | docs link、CLI recipe、legacy semantics、lineage、skill inventory/parity |

## Local verification

records / schema / Python 実装を変更したら最低限以下を実行します。

```bash
uv run baibai-loop-validation
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

CI と完全に揃える場合は [`python-foundation.md`](./python-foundation.md) §9 を参照します。

## Docs path checks

docs 再編や link 更新をした場合は、少なくとも以下を確認します。

```bash
rg -nP '\]\((?!https?://|#|/)[^)]+\.md\)' docs README.md AGENTS.md
rg -n 'docs/' src tests records/_schemas
```

relative Markdown links は GitHub 上で解決される path かを確認します。旧 path shim を削除する前には、repo 全体で旧 path の参照が消えていることを `rg` で確認します。

## Behavior asset drift

docsとskillはAIの挙動を変えるproduction assetとしてreviewする。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python tools/drift/check_markdown_links.py
UV_CACHE_DIR=/tmp/uv-cache uv run python tools/drift/check_cli_doc.py
UV_CACHE_DIR=/tmp/uv-cache uv run python tools/drift/check_legacy_semantics.py
UV_CACHE_DIR=/tmp/uv-cache uv run python tools/drift/check_duplicate_constants.py
UV_CACHE_DIR=/tmp/uv-cache uv run python tools/drift/check_skill_inventory.py
```

`check_skill_inventory.py`は`.agents/skills`が`decision-cycle / macro-analysis / improvement-loop / tradingview-open`のexact 4件、`.claude/skills`が各canonical directoryへのrelative symlinkであることを検証する。canonical tree内のsymlink、frontmatter name/description不正、`agents/openai.yaml`の必須interface/policy不正、旧skill directory、別実体copyを許さない。

decision-cycleのcopy/paste recipeは`tests/test_public_cli_contract.py`で各subcommandとoptionをpublic parserへ渡し、同じcommand/optionがrunbookに存在することを固定する。commandを変更するときはparser、runbook、contract testを同じPRで更新する。

legacy semantics gateは削除済みposition Markdown、旧thesis memo、`durability_gate`、価格stop/targetの売買指示、realtime/broker/lifecycle二重記録をcurrent instructionとして要求する文章を拒否する。禁止規範そのものはpath+patternの限定allowlistにし、単語全体やdirectory全体を除外しない。
