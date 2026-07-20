---
title: "Python foundation"
summary: "Python の runtime・依存管理・lint・型検査・validation 境界・テスト・セキュリティ・CI 一致の正本。"
doc_type: reference
status: active
last_reviewed: 2026-07-20
source_paths:
  - "../../src/baibai_engine/"
  - "../../tests/"
  - "../../pyproject.toml"
  - "../../uv.lock"
---

# Python foundation

このリポジトリの Python 基盤の正本。対象は `src/baibai_engine/**` と `tests/**`。Baibai-Loop は外部データを取り込み、Markdown front matter と cache に永続化し、売買判断の事実レイヤーを作るため、Python 基盤では「新しさ」よりも **境界が検証され、静的に読め、CI で再現できること** を優先する。

## 1. Runtime policy

- Python は `>=3.14,<3.15` の単一ターゲットとする。
- `.python-version` は実行確認済みの patch release に固定する。
- 後方互換のための分岐は置かない。古い Python への配慮より、型構文・標準ライブラリ・ツール設定を単純に保つ。

参考:

- https://devguide.python.org/versions/
- https://peps.python.org/pep-0745/

## 2. Dependency policy

依存管理は `uv` に寄せる。`pyproject.toml` と `uv.lock` を単一の真実源にし、`requirements.txt` は常設しない。

依存の置き場所は以下に分ける。`pyproject.toml` では `[dependency-groups]` テーブル直下に `test` / `typing` / `quality` / `security` / `dev` をリストとして並べる。

- `[project.dependencies]`: 実行時に必要な依存だけ。
- `[dependency-groups]` の `test`: pytest / coverage / Hypothesis などテスト用。
- `[dependency-groups]` の `typing`: mypy と stub。
- `[dependency-groups]` の `quality`: Ruff / pre-commit / mutmut。
- `[dependency-groups]` の `security`: Bandit / pip-audit。
- `[dependency-groups]` の `dev`: 上記 group の include だけ。

経験的に、dev tooling を optional dependencies に入れると「配布 extras」と「ローカル開発環境」が混ざる。uv は standardized dependency groups を扱えるため、この repo では optional dependencies を開発用途に使わない。

`dev` は include-only にしてあるが、CI / ローカルでは明示的に `uv sync --frozen --all-groups` と `uv export --all-groups` を使う。group 構成を将来変更したときに `dev` の include 漏れで取りこぼすリスクを排除し、CI 上での group 選択意図をコマンド側に残すためである。

Dependabot は `package-ecosystem: "uv"` を使う。uv lockfile 更新に対応しているが未対応ケースが残るため、依存更新 PR は CI の `uv sync --frozen --all-groups` を必ず見る。

参考:

- https://docs.astral.sh/uv/concepts/projects/dependencies/
- https://docs.astral.sh/uv/guides/integration/dependabot/

## 3. Formatting and linting

formatter と linter は Ruff に統一する。Black / isort / Flake8 / pyupgrade を重ねない。

採用理由:

- Ruff は Python 3.14 対応を明示している。
- formatter と linter の target-version を 1 箇所で固定できる。
- import sort と formatting を同じ toolchain で扱える。
- pre-commit と CI のコマンドを短く保てる。

この repo 固有の設定:

- runtime は Python 3.14 だが、Ruff は `target-version = "py313"` に固定する。Ruff の Python 3.14 formatter は PEP 758 の `except T1, T2:` パーレス構文へ自動整形するため、複数例外捕捉を `except (T1, T2):` に統一する目的で 3.13 target を使う。
- line length は 100。
- `RUF001` は無効化する。日本語の docstring や出力文字列リテラルでは全角括弧・ギリシャ文字がドメイン表現として自然に出るため、ambiguous unicode を一般ルールとして禁止すると false positive が多い。
- tests は `ANN` / `PT009` / `PT027` などを緩める。テストは既存の `unittest` 形を維持しつつ、production code の strictness を優先する。
- Markdown では末尾スペースが hard break として使われるため、pre-commit の trailing whitespace hook は `.md` を除外する。`end-of-file-fixer` は EOF 改行のみ補正し本文の hard break には触らないので、Markdown 全般を対象にしたままで安全。
- 複数例外捕捉は `except (T1, T2):` と書く。commit 前に `rg -n "except [A-Za-z0-9_.]+, [A-Za-z0-9_.]+" src tests` が 0 件であることを確認する。

参考:

- https://docs.astral.sh/ruff/
- https://docs.astral.sh/ruff/formatter/

## 4. Type checking

mypy strict を CI の主 type gate とする。Pyright の設定ファイルは repo に置かず、IDE での利用は開発者裁量に委ねる。CI の type gate は mypy 一本に揃え、CI 結果と IDE 表示の乖離は許容する。

採用設定の意図:

- `strict = true`: 未型付け関数、未型付け呼び出し、暗黙 Optional などをまとめて禁止する。
- `warn_unreachable = true`: CLI 分岐や Protocol 変更で dead path を見つける。
- `disallow_any_unimported = true`: stub 不足による stealth Any を検出する。
- `plugins = ["pydantic.mypy"]`: Pydantic model の constructor と field 定義を mypy に理解させる。
- `py.typed`: package consumer に型付き package として公開する。

外部 SDK は完全な型を持たないことがある。`jquantsapi.*` などは override で missing import を許容するが、その Any は provider module の中で止める。application 層へは `Protocol` と domain model を通して渡す。

参考:

- https://mypy.readthedocs.io/
- https://docs.pydantic.dev/latest/integrations/mypy/

## 5. Validation boundary

Pydantic は「外部から入る値」と「永続化境界」に使う。すべての内部値を Pydantic model にするのではなく、次を境界とする。

- environment config
- external provider payload normalization
- Markdown front matter parsing
- screen result document serialization

この repo では Pydantic の strict mode を基本にする。ただし YAML / API / cache から来る raw 値は、normalizer で明示的に変換してから domain model に渡す。例えば `str -> date` や `str -> float` は provider normalizer の責務であり、domain model に暗黙 coercion させない。

この方針により、次の事故を避ける。

- `0` / `0.0` を falsy として欠損扱いする。
- `NaN` が valuation 計算へ流れる。
- ticker 表記揺れが downstream key に混ざる。
- front matter の壊れた shape を select 処理が黙って空扱いする。

参考:

- https://docs.pydantic.dev/latest/concepts/strict_mode/
- https://docs.pydantic.dev/latest/concepts/config/

## 6. I/O and adapters

CLI は orchestration に寄せ、外部境界は provider `Protocol` で切る。テスト fake は Protocol を満たせばよく、具象 provider の private 実装に依存しない。

ファイル書き込みは `write_text_atomic()` を使う。screening runのYAML viewは運用上の成果物なので、途中でプロセスが落ちても partial file を残さないことを優先する。

provider では次を守る。

- 外部 exception は secret を含む可能性があるため、表示前に sanitize する。
- retry は transient HTTP status に限定する。
- cache payload は `json.loads()` 後に shape を検証する。
- HTML scraping の構造抽出（table / link / 見出し）は regex ではなく `HTMLParser` を介す。テキスト後処理（空白正規化、日付抽出など）で `re` を併用するのは妨げない。

## 7. Testing and coverage

pytest は CI / config / marker / xfail の strict 系を個別に有効化する。`addopts` に `--strict-config` と `--strict-markers` を入れ、ini で `xfail_strict = true` を設定する。`--strict` の集約 alias は pytest 9 では曖昧になるため使わず、明示指定で厳密度の意図を保つ。

coverage は `coverage.py` を直接使う。pytest-cov は便利だが、この repo の CI では `coverage run -m pytest` と `coverage report` で足りる。

coverage gate は現在 80%。これは理想値ではなく、既存 suite の実測に合わせた初期 baseline である。今後は以下の順で ratchet する。

1. provider の cache / error path を追加テストする。
2. CLI parser と subcommand error path を追加テストする。
3. 変更行 coverage を PR レビュー観点に入れる。
4. total gate を 85%、90%、95% の順に上げる。

参考:

- https://docs.pytest.org/en/9.0.x/
- https://coverage.readthedocs.io/en/latest/config.html

## 8. Security checks

CI では Bandit と pip-audit を分ける。

- Bandit: source code の危険な構文や API を見る。
- pip-audit: lock 由来の依存を requirements に export して監査する。

CodeQL は採用しない。GitHub の Code scanning は private repository では Advanced Security ライセンス（Organization 限定の有償機能）が必須で、個人 plan の private repo では SARIF の取り込み先がない。SARIF を artifact として保存する形でも結果の検査体験が貧弱で運用価値が薄いため、CodeQL ジョブは置かず、代替として上記 2 ツールに集中する。

`pip-audit --locked .` は uv の `uv.lock` を直接拾えないため、CI では以下の順にする。

1. `uv export --format requirements.txt --locked --all-groups --no-emit-project --no-hashes`
2. `pip-audit -r <exported requirements>`

`pip-audit --local` は実行環境の `pip` 自体も監査対象に含めるため、project dependency ではない pip の CVE で CI が落ちることがある。repo の依存監査としては lockfile export を正とする。

参考:

- https://github.com/pypa/pip-audit

## 9. CI and local parity

CI と local は同じコマンドを使う。

```bash
uv sync --frozen --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run coverage run -m pytest
uv run coverage report -m
uv run bandit -c pyproject.toml -r src/baibai_engine -q
uv export --format requirements.txt --locked --all-groups --no-emit-project --no-hashes --output-file /tmp/baibai-loop-requirements.txt
uv run pip-audit -r /tmp/baibai-loop-requirements.txt
uv build --wheel
```

GitHub Actions では `astral-sh/setup-uv` を使う。`python -m pip install uv` より CI の intent が明確で、uv cache も扱いやすい。

参考:

- https://docs.astral.sh/uv/guides/integration/github/

## 10. Review rule

Python 基盤を変える PR は、ツール設定だけを見て終わらせない。最低限、次を見る。

- `uv lock` が更新され、`uv sync --frozen --all-groups` で再現できるか。
- `ruff format --check` と `ruff check` が CI と local で同じか。
- mypy strict のために `Any` が application 層へ漏れていないか。
- Pydantic が内部計算の代替になっていないか。validation boundary に留まっているか。
- security audit が実行環境ではなく project dependency を見ているか。
- pre-commit が docs の意味ある whitespace を壊さないか。
- coverage gate が現実に通り、かつ上げる余地を明示しているか。

この repo では、Python のモダナイゼーションは「新しいツールを足すこと」ではなく、**人間の判断が混ざる trading loop の事実生成部分を、壊れた入力・曖昧な型・部分書き込み・CI 非再現性から守ること** と定義する。
