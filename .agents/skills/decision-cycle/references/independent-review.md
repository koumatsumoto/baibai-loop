# Independent Investment Review

packet authorと別roleで実行し、packetを直接編集しない。

## Input

- audit poolとshortlist比較
- packet path/core hash
- source URL、公表日、対象期
- current ledger annotation
- plan-limit output（存在する場合）

## 必須反証

1. 上位候補を都合よく除外していないか。
2. 構造的衰退を一時的割安と誤認していないか。
3. scenario、FV、CAGR、share count、dividendを再計算したか。
4. 永久損失7軸のunknown/adverseを一次sourceで照合したか。
5. より良い代替候補を見落としていないか。
6. repository portfolioへのmarginal valueはあるか。
7. 予算、保有、予約でrankingを歪めていないか。
8. limit/max price/quantity/notionalがpacketと一致するか。

## Output

decision-review schemaのdraftだけを返す。`proposal_changed=true`なら理由を付けてpacket authorへ戻す。packet変更後は新hashにreviewを再生成する。repo外資産配分、hedge、単元未満株等へscopeを広げない。
