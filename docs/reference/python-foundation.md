---
title: "Python foundation"
summary: "Python の runtime・依存管理・lint・型検査・validation 境界・テスト・セキュリティ・CI 一致の正本。"
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

このリポジトリの Python 基盤の正本。対象は `engine/src/baibai_engine/**`、`web/backend/src/baibai_web/**`、`batch/src/baibai_batch/**` と `tests/**`。Baibai Loop は外部データを取り込み、SQLite store と cache に永続化し、売買判断の事実レイヤーを作るため、Python 基盤では「新しさ」よりも **境界が検証され、静的に読め、CI で再現できること** を優先する。

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
- `[dependency-groups]` の `quality`: Ruff / import-linter / pre-commit。
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
- pre-push hook は §9 と同じ 14 gate を同じ形で回し、その後に `pytest -q -m "not slow"` を回す。drift gate は pytest の中では走らない — CI の Drift gates step と §9 が正本で、pytest に同じ実行を持たせると同じ入力を 2 度検査することになる。

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
- `packages = ["baibai_engine", "baibai_web", "baibai_batch", "tools"]`: 3 runtime package と developer tool を同じ strict gate に置く。file 名の列挙にすると、追加した module が誰かに思い出されるまで無検査で残る。
- `exclude`: `tools/generators/generate_brand_assets.py` だけを外す。この script は Pillow を PEP 723 の inline metadata で宣言して `uv run --script` で動くため、native image library を shared lock と全 workflow の install から外している。その代償として import が解決できない。
- `py.typed`: package consumer に型付き package として公開する。

外部 SDK は完全な型を持たないことがある。`jquantsapi.*` などは override で missing import を許容するが、その Any は provider module の中で止める。application 層へは `Protocol` と domain model を通して渡す。

参考:

- https://mypy.readthedocs.io/
- https://docs.pydantic.dev/latest/integrations/mypy/

## 5. Validation boundary

Pydantic は「外部から入る値」と「永続化境界」に使う。すべての内部値を Pydantic model にするのではなく、次を境界とする。

- environment config
- external provider payload normalization
- application DB / SQLite store の read/write 境界
- screen result document serialization

この repo では Pydantic の strict mode を基本にする。ただし YAML / API / cache から来る raw 値は、normalizer で明示的に変換してから domain model に渡す。例えば `str -> date` や `str -> float` は provider normalizer の責務であり、domain model に暗黙 coercion させない。

この方針により、次の事故を避ける。

- `0` / `0.0` を falsy として欠損扱いする。
- `NaN` が valuation 計算へ流れる。
- ticker 表記揺れが downstream key に混ざる。
- run store(SQLite) の壊れた shape を select 処理が黙って空扱いする。

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

suite は pytest-xdist の worker で並列実行する。`TMPDIR` を tmpfs へ寄せてから I/O 待ちは消え、現在は CPU-bound である（`ci.yml` の該当 comment が実測を持つ: 2 core runner で CPU/wall ≈ 2.0）。待ちを埋める余地が無いので、core 数を超える worker は memory と切り替えに負けるだけになる。`addopts` の `-n auto` は開発機の広さを使うための既定で、CI は runner 実測で選んだ固定値を渡す。2 core の runner では worker 8 が 92 秒・16 が 130 秒と明確に悪化し、2 と 4 はどちらも runner のばらつき（74〜95 秒）の中に入る。過剰にしない側の 4 を取る。単一 process が要る実行（`-s`、`--pdb`、逐次の進捗表示）は `-n 0` で戻す。

coverage は pytest-cov 経由で計測する。pytest-cov は各 worker の中で coverage を開始するのに対し、`coverage run -m pytest` は controller process しか見ず、並列実行では空に近い結果を報告する。計測値は直列実行と一致する。

coverage gate は現在 80%。これは理想値ではなく、既存 suite の実測に合わせた初期 baseline である。上げるのは行数ではなく検出力を上げたときで、その判断は次の所有権規則で行う。

### 7.1 どの層が何を所有するか

1 契約 = 1 primary owner。owner はその契約を実装する最下層の module とし、上位層はそこへ配線されていることだけを見る。

- **R1 所有権**: owner 以外が同じ値・同じ述語を assert している箇所は、配線を示す 1 assert を残して literal を削る。
- **R2 上位層の予算**: CLI / batch / Web / export が持つのは option・default・exit code・public output・配線と、代表的な正常系 1 本と fail-close 1 本。domain の全 partition を上位層で繰り返さない。
- **R3 payload 所有**: consumer は自分が所有する field だけを assert する。payload 全文の比較は serializer round-trip の owner 1 箇所に置く。
- **R4 同型**: 入力が 1 つだけ違う test が 3 件以上あれば 1 つの table にする。行（case）は減らさず、各行に元の test 名由来の id とその行がある理由を残す。`addopts` に `--maxfail=1` があるため、行ごとの失敗を全部報告する `subtests`（pytest 9 組み込み）を第一候補にし、行ごとに独立した fixture が要るときだけ `parametrize` を使う。table の行は production の定数から導かず literal で書く — 定数から導いた行はその定数と一緒に動き、定数の変更を検出できない。
- **R5 builder**: 同じ概念の fixture builder は `tests/helpers/` に 1 つだけ置く。default が違う 2 つ目は variant ではなく bug である。builder が出せる形は production の writer が受理する形と一致させる。これは両向きに効く: production が拒否する形を正常系 fixture にしないのと同じだけ、**production が出す形を builder が書けなくしない**。field 間の関係を builder 側で導出すると後者が起きる — 導出した不変条件が production のものでなければ、その組み合わせを持つ回帰は fixture に書けず、永久に検出できない。

削除・統合は「`rg` で 0 件」では決めない。消して full suite を回し、対象の guard を 1 行壊して retained test が赤になることを確かめてから確定する。

参考:

- https://docs.pytest.org/en/9.0.x/
- https://coverage.readthedocs.io/en/latest/config.html

## 8. Security checks

CI では Bandit、pip-audit、npm audit を分ける。

- Bandit: source code の危険な構文や API を見る。
- pip-audit: lock 由来の依存を requirements に export して監査する。
- npm audit: UI / Worker の各 lockfile を監査し、high / critical advisory を拒否する。

CodeQL は採用しない。GitHub の Code scanning は private repository では Advanced Security ライセンス（Organization 限定の有償機能）が必須で、個人 plan の private repo では SARIF の取り込み先がない。SARIF を artifact として保存する形でも結果の検査体験が貧弱で運用価値が薄いため、CodeQL ジョブは置かず、代替として上記 3 ツールに集中する。

`pip-audit --locked .` は uv の `uv.lock` を直接拾えないため、CI では以下の順にする。

1. `uv export --format requirements.txt --locked --all-groups --no-emit-project --no-hashes`
2. `pip-audit -r <exported requirements>`

`pip-audit --local` は実行環境の `pip` 自体も監査対象に含めるため、project dependency ではない pip の CVE で CI が落ちることがある。repo の依存監査としては lockfile export を正とする。

scanner は「答えが何によって動くか」で置き場所が決まる。Bandit は pinned version で source を読むので、答えは変更でしか動かない。pull request 上の ci job が唯一の実行点で、週次に置いても新しい発見は出せない。advisory は repository が動かないまま公表されるので、pip-audit と npm audit は変更時に加えて週次 sweep でも実行する。Node audit の変更時実行は `.github/workflows/node-audit.yml` が担い、trigger は両 lockfile 自身に絞る。この検査の answer は repository の外（GitHub advisory database）で動くので、`web/frontend/` の変更すべてを対象にすると他人の公表が無関係な UI 作業を止め、production を publish する job に置くと緊急修正の publish を止める。ここが拒否するのは「高 severity の advisory を連れてきた resolution」だけで、止まったままの lockfile に後から出る advisory は週次 sweep の findings である。監査は `--package-lock-only` で install を重複させず、lockfile の再現性と実動互換性は web job の `npm ci` 以降が検証する。

参考:

- https://github.com/pypa/pip-audit

## 9. CI and local parity

CI と local は同じコマンドを使う。

```bash
uv sync --frozen --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run lint-imports
for gate in tools/quality/drift/check_*.py; do uv run python -m "tools.quality.drift.$(basename "$gate" .py)"; done
TMPDIR=/dev/shm uv run pytest -n 4 --cov --cov-report=term-missing
uv run --with pillow python -c 'import PIL.Image'
uv run --with pillow pytest -n 0 tests/tools/test_brand_assets.py
uv run bandit -c pyproject.toml -q -r engine/src/baibai_engine web/backend/src/baibai_web batch/src/baibai_batch tools
uv export --format requirements.txt --locked --all-groups --no-emit-project --no-hashes --output-file /tmp/baibai-loop-requirements.txt
uv run pip-audit -r /tmp/baibai-loop-requirements.txt
distribution_dir="$(mktemp -d)"
uv build --wheel --sdist --out-dir "$distribution_dir"
wheel_path="$(find "$distribution_dir" -maxdepth 1 -type f -name '*.whl' -print -quit)"
sdist_path="$(find "$distribution_dir" -maxdepth 1 -type f -name '*.tar.gz' -print -quit)"
uv run python tools/quality/check_distribution.py --wheel "$wheel_path" --sdist "$sdist_path"
distribution_venv="$(mktemp -d)"
uv venv "$distribution_venv"
uv pip install --python "$distribution_venv/bin/python" "$wheel_path"
"$distribution_venv/bin/python" tools/quality/check_distribution.py --installed
```

brand asset の 2 行が `--with pillow` を挟むのは、Pillow を lock の外に置いているためである。通常の `pytest` では `tests/tools/test_brand_assets.py` が `importorskip` で丸ごと skip され、破れが緑のまま通る。import できることを先に確かめてから走らせて、この fail-open を塞ぐ。

Node dependency gate は各 lockfile を直接監査する。

```bash
cd web/frontend
npm audit --package-lock-only --audit-level=high
cd ../edge
npm audit --package-lock-only --audit-level=high
```

UI（`web/frontend/`）と Cloudflare Worker（`web/edge/`）は、同じ web job で次の順に検証する。UI は依存を 1 回だけ install して lint / build / test を通し、その build artifact を含む checkout のまま Worker の型生成・型検査・test・dry-run bundle を検証する。

```bash
cd web/frontend
npm ci
npm run lint
npm run build
npm test
cd ../edge
npm ci
npm run types:check
npm run typecheck
npm test
npx wrangler deploy --dry-run --outdir /tmp/baibai-worker-bundle
```

この §9 は gate の唯一の正本で、Python quality（Bandit と pip-audit を含む）と notification script の stdlib-only import contract は `.github/workflows/ci.yml`、UI / Worker と production deploy は `.github/workflows/web.yml`、Node lockfile の advisory は `.github/workflows/node-audit.yml`、依存 advisory の週次 sweep は `.github/workflows/security.yml` を正本とする。workflow は 1 つずつ「trigger が担当範囲を宣言する」形にし、job 内で範囲を測り直さない。

Actions は job 単位で分単位切り上げ課金されるため、gate の構成は 1 run の速さではなく run 数と job 数で決める。Python gate は 1 job に同居し、環境構築を 2 度払わない。ci は pull request と manual dispatch に答え、main push では走らない。pull request が検査する merge ref は、base が動かない限り main へ載る tree そのもので、merge 後の再実行は同じ byte に課金するだけだからである。

この構成が残す穴は「run 後に base が動いてから merge した tree」で、GitHub は base の前進では pull request workflow を再実行しない。plan 上 branch protection（up-to-date 必須）が使えないため強制もできない。この tree を再検査するのは次の pull request の merge ref で、daily batch が毎営業日確認するのは main が import して走ることだけである。lint / 型 / test だけの破れは次の pull request まで残る。credential 境界だけは待たせずに済ませるため、週次 sweep が main に対して `check_workflow_trust` を実行する。main push を保つのは web だけで、その push は検査ではなく production を publish する操作である。

web の trigger は `paths` filter に置き、run が存在すること自体が web tree の変化を意味する。`push` の filter は `wrangler deploy` が publish する tree（`web/frontend/**`・`web/edge/**`）に一致させ、CI file だけの編集が production を publish しないようにする。`pull_request` はそこへ自身の定義 file を加え、workflow 自身の変更もその workflow で通す。GitHub は filter を diff 先頭 3,000 file までで評価し、一致 file がその窓の外にあると workflow を無音で skip する。diff は「前の tree の file + 後の tree の file」で抑えられるので、各 commit が tracked file の budget を守る限り 3,000 に届かない。budget は `tests/batch/test_cloud_workflow_contracts.py` が保ち、超えるときは filter を job 内判定へ戻す。

pull request の同一 workflow は新しい commit が来たら旧 run を cancel し、schedule と deploy 経路は互いに cancel しない。concurrency group は pending run を 1 本しか保持しないので、deploy に到達し得ない run（main 以外を指した manual dispatch）は production group へ入れない。入れると待機中の実 deploy を追い出し、production が main より古いまま緑の run だけが残る。deploy は `push` と `workflow_dispatch` を名指しで許可する。`github.ref` は schedule や workflow_run でも default branch になるため、「pull request でない」という条件では後から足した trigger に production を渡してしまう。deploy は同じ job の UI / Worker gate と dry-run が成功した main の run だけで実行し、deploy 直前に remote `main` と対象 run の `web/frontend/`・`web/edge/` tree を再照合して、後続 web 変更がある run は deploy しない。Cloudflare credential は deploy step だけへ渡す。

Bandit と pip-audit は ci の 1 job に同居するが `!cancelled()` を付ける。runner を 2 台に増やさないために同じ job へ置くのであって、赤い test が CVE を隠してよいわけではない。同じ理由で週次 sweep も両 ecosystem を必ず問う。`README.md` / `AGENTS.md` はローカル用の subset だけを載せてここを参照する。GitHub Actionsでは`astral-sh/setup-uv`を使い、root `pyproject.toml`のexact `tool.uv.required-version`を全workflowの正本とする。`python -m pip install uv`よりCIのintentが明確で、uv cacheも扱いやすい。

参考:

- https://docs.astral.sh/uv/guides/integration/github/
- https://docs.github.com/en/actions/how-tos/troubleshoot-workflows#filtering-and-diff-limits

全workflowの外部Actionは上流releaseのfull commit SHAへ固定し、同じ行のコメントにrelease tagを残す。repository Actions設定のSHA pin enforcementと`tools/quality/drift/check_workflow_trust.py`を併用し、tag/branch参照、`run:`へのdispatch input直接展開、credentialのjob scope化を拒否する。Dependabotの更新でも、上流releaseとcommitの対応を確認してgateのallowlistとworkflowを同時に更新する。

`lake-acceptance.yml`はacceptance credentialを持つため、gateが文書全体のSHA-256をpinし、どの変更もレビューを通す。この`_LAKE_ACCEPTANCE_WORKFLOW_DIGEST`はworkflowを変更するたびに更新する。gateは型の暗黙変換を避けるため`yaml.BaseLoader`で読むので、digestも同じloaderで算出する。

```bash
uv run python -c "
import yaml
from pathlib import Path
from tools.quality.drift.check_workflow_trust import _mapping_digest
print(_mapping_digest(yaml.load(Path('.github/workflows/lake-acceptance.yml').read_text(), Loader=yaml.BaseLoader)))
"
```

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
