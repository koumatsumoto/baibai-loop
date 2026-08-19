# R2 Bucket Lock の設定記録（2026-08-19）

`baibai-stores` の immutable prefix に age-based Bucket Lock（90 日）を設定した。
設計と対象 prefix の正本は [`docs/reference/market-lake.md`](../../../docs/reference/market-lake.md)、
key の正本は `engine/src/baibai_engine/market/lake/keys.py`。

実行環境: `wrangler 4.114.0`（`web/edge` の devDependency に pin）、OAuth 認証。

## before

```
$ wrangler r2 bucket lock list baibai-stores
There are no lock rules for bucket 'baibai-stores'.

$ wrangler r2 bucket lifecycle list baibai-stores
name:     Default Multipart Abort Rule
enabled:  Yes
prefix:   (all prefixes)
action:   Abort incomplete multipart uploads after 7 days
```

lifecycle rule は未完了 multipart の中断だけで object を削除しないため、lock と競合しない。
`baibai-serving` 側は `candidate-views-expire-31d`（`history/candidate-views/`、31 日）と
`longlist-history-expire-400d`（`history/longlists/`、400 日）で、どちらも serving bucket なので対象外。

## canary（実データに触れない可逆性と enforcement の実証）

probe 専用 prefix `lake/l1/canonical/.lock-probe/` に retention 1 日の rule を置いて確認した。
実 key は `lake/l1/canonical/{dataset}/...` なので dataset 名と衝突しない。

| 手順 | 期待 | 観測 |
| --- | --- | --- |
| rule 追加（`--retention-days 1 --force`） | 成功 | `✨ Added lock rule 'lock-canary'` |
| sentinel の PUT | 成功 | ETag `"7eb880c1…"` |
| sentinel の DELETE | **拒否** | `ObjectLockedByBucketPolicy` / exit 254 |
| sentinel の上書き PUT | **拒否** | `ObjectLockedByBucketPolicy` / exit 254 |
| rule 削除（`lock remove --name`） | 成功 | `Lock rule 'lock-canary' removed` |
| 削除後の DELETE | 成功 | exit 0、`head-object` で absent |

lock は削除と上書きの両方を止め、rule の削除でその強制が解ける（S3 の compliance mode と異なり可逆）。
`lock remove` に確認 skip の flag は無いが、非対話実行でそのまま完了する。

## after

```
$ wrangler r2 bucket lock add baibai-stores l1-canonical-90d "lake/l1/canonical/" --retention-days 90 --force
$ wrangler r2 bucket lock add baibai-stores l1-manifests-90d "lake/manifests/" --retention-days 90 --force

$ wrangler r2 bucket lock list baibai-stores
name:       l1-canonical-90d
enabled:    Yes
prefix:     lake/l1/canonical/
condition:  after 90 days

name:       l1-manifests-90d
enabled:    Yes
prefix:     lake/manifests/
condition:  after 90 days
```

`lake/pointers/`（mutable な CAS pointer）、`lake/staging/`（GC が回収する in-flight 領域）、
bucket 直下の store key と `.bak*` は対象外のまま。

## 実 rule の enforcement probe

各 prefix に sentinel を置いて DELETE を試し、prefix の綴りが効いていることを確認した。

| key | DELETE |
| --- | --- |
| `lake/manifests/.lock-probe/probe.json` | `ObjectLockedByBucketPolicy` / exit 254 |
| `lake/l1/canonical/.lock-probe/probe.json` | `ObjectLockedByBucketPolicy` / exit 254 |

sentinel は 90 日後に削除できる。後始末は dated task `task-20260819-ops-10`（due 2026-11-18）。

## 日次経路との整合

publish は既存 key へ再 PUT しない（`If-None-Match: *` + metadata 証明）ので、上書き拒否と衝突しない。
R2 側の lake object 削除は自動経路に存在しない — `market/lake/retention.py` の GC は
`mirror_path(...).unlink()` で **local mirror だけ**を削除し、呼び出し元は `lake write-cli` の手動 subcommand
だけである。`r2_transfer.sh` の `delete-object` は bucket 直下の `baibai.sqlite.bak-YYYYMMDD` を prune する
経路だけで、locked prefix を指さない。

設定後の定時 daily 1 回の緑を観測して完了とする（dated task `task-20260819-ops-11`）。lock 起因で赤に
なった場合の rollback は `wrangler r2 bucket lock remove baibai-stores --name l1-canonical-90d`（+ manifests
側）で、canary で有効性を実証済み。
