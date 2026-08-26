# web/contracts

UI と backend が同じ形を二重管理しないための、check-in された契約。

| file | 何を決めるか | 誰が書くか |
| --- | --- | --- |
| `routes.json` | `/api` の route と serving artifact の対応 | 人間 |
| `read-model.schema.json` | serving artifact の JSON 構造 | 生成物 |

## read-model.schema.json と UI の型

正本は `web/backend/src/baibai_web/readmodel/models.py` の Pydantic model である。`materialize`
がその model から `views/*.json` を書き、local API が同じ形で答え、UI がそれを読む。UI の型を手で
書くと 2 つの記述が「誰かが覚えている限り」でしか一致せず、backend の field rename は次の batch で
本番へ出る一方、UI は無くなった key を読み続ける。

そこで UI の型は model から生成し、間に置く JSON Schema を review 可能な artifact として
check-in する。生成するのは次の 2 file:

- `web/contracts/read-model.schema.json`
- `web/frontend/src/api/types.ts`

```bash
uv run python -m baibai_web.contracts_export          # 生成して書き出す
uv run python -m baibai_web.contracts_export --check  # 差分があれば exit 1
```

model を変えたら生成し直して同じ commit に入れる。忘れた場合は
`tools/quality/drift/check_readmodel_contract.py`（CI の drift gate）が赤くなる。生成し直した後は
UI が新しい型で compile するかを `npm run build` で確かめる — field rename を読んでいた箇所は
そこで名指しされる。

`schema` は artifact を記述するので、property は全て `required` である。`model_dump_json` も
FastAPI の `response_model` も field を省かないため、published JSON には常に全ての key が居る。
唯一の例外は code deploy と materialize の間の窓で、そこは runbook の順序と UI の graceful
degrade が扱う（`batch/OPERATIONS.md`）。

## 生成に含まれないもの

`/api/health` は plain dict を返すので view model を持たない。
