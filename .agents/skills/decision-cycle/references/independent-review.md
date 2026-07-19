# Independent Investment Review

packet authorと別roleで実行し、packetを直接編集しない。

## Input

- audit poolとshortlist比較
- packet path/core hash
- source URL、公表日、対象期
- `baibai-engine research evaluate <packet>`の`five_year_base_break_even`
- current ledger annotation
- plan-limit output（存在する場合）

## 必須反証

1. 上位候補を都合よく除外していないか。
2. 構造的衰退を一時的割安と誤認していないか。
3. scenario、FV、CAGR、share count、dividendを再計算したか。
4. 5年baseのterminal multipleとearnings growthについて、base値、break-even値、downside bufferを`scenario.base_3y_5y` checkへ記録したか。
5. 観測trailing multipleのfact ID、値、baseとの差を同じcheckへ記録したか。anchorが一意に解決しない場合は`complete`にしない。
6. base terminal multipleが観測値を上回る場合、premiumを支えるpacket内fact IDと一次source IDを記録し、`decision_impact`で維持または修正理由を説明したか。数値の余裕だけを根拠にしない。
7. 永久損失7軸のunknown/adverseを一次sourceで照合したか。
8. より良い代替候補を見落としていないか。
9. repository portfolioへのmarginal valueはあるか。予算、保有、予約でrankingを歪めていないか。
10. limit/max price/quantity/notionalがpacketと一致するか。

review開始時に`scenario.base_3y_5y` checkを`pending`へ戻す。`note`へbase / break-even / bufferの数値と観測fact IDを単位付きで書き、`source_ids`へpremiumを支える一次source IDを置く。`decision_impact`には仮定を維持・修正する理由とproposalへの影響を書く。fact IDを`source_ids`へ入れない。これらを確認した後だけ`complete`へ戻す。downside bufferが0以下でも計算と判断影響を確認できればreviewは完了できるが、買い提案と必要利回りが整合しなければ`proposal_changed=true`とする。

## Output

decision-review schemaのdraftと、更新した既存research checklistだけを返す。break-even計算、観測anchor、または一次情報によるpremium根拠を確認できない場合はcheckを`blocked`にし、理由と必要なpacket修正を`decision_impact`へ書く。scenarioまたはproposalを変える場合は`proposal_changed=true`としてpacket authorへ戻す。packet変更後は新hashにreviewを再生成する。repo外資産配分、hedge、単元未満株等へscopeを広げない。
