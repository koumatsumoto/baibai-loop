---
title: "Architecture"
summary: "packageの責務・依存、情報層、store authority、実行環境と無人経路の停止方針。"
doc_type: architecture
status: active
---

# Architecture

Baibai Loopは単一distribution内で、domainを所有する`baibai_engine`、読み取り専用の表示を担う`baibai_web`、定期処理と転送を担う`baibai_batch`を分ける。開発・実行環境はUbuntu Linuxを対象とし、POSIX pathとGNU/Linuxのfilesystemを前提にする。

<a id="layers"></a>

## 1. 構造と4役

engineはweb・batch・toolsへ依存しない。Webからengineへの入口は`read_api`、batchからは`batch_api`と`read_api`であり、import-linterが境界を検査する。batchはdomainの判断やwrite invariantを独自実装せず、engineのserviceへ委譲する。storeとserving objectの転送はbatchが担う。

<a id="four-roles"></a>

機構の役割は、業務の成果を「産む」、誤った出力を「止める」、結果を「測る」、判断材料を「見せる」で説明する。採否は[開発原則](./doctrine.md#development-investment-policy)に従う。この分類を全moduleに定型文として記載する義務は設けない。

## 2. 業務と人間gate

成果物と人間判断の関係は[Decision flow](./domain-language.md#decision-flow)、実行する操作は[目的別入口](./README.md#目的別の入口)から該当skillを参照する。

<a id="information-layers"></a>

## 3. 情報の分類

| 区分 | 内容 |
| --- | --- |
| L1 fact | sourceから取得した観測 |
| L2 machine | 保存入力とmethodから導出する機械出力 |
| L3 application data | 判断、確認済み取引事実、task・Operation等の運用状態 |
| method / config | Git管理の計算規則、調査手法、表示設定 |

L3に保存した取引事実はAIの判断ではない。また、現在のsourceから再取得できることは、過去の改定前状態の再現を意味しない。再生成可能なL2に新たな永久保存を要求せず、復旧元と保持範囲は各storeの契約に従う。観測・導出・見積り・判断の区別は[doctrine](./doctrine.md#fact-analysis-separation)が所有する。

### Store authority

| 保存場所 | 正本として所有する情報 | write owner |
| --- | --- | --- |
| `stores/application/baibai.sqlite` | 判断、ledger、task、Operation、outcome。localが正本 | engine application service |
| R2 `lake/` | lake所有datasetのimmutable object、manifest、current pointer | `publish-lake` |
| `stores/market/market.sqlite` | lake所有tableはruntime copy。`source_coverage`と`tse_capital_policy_snapshots`はstore-localの正本、`lake_store_origin`はlocal metadata | market/screening provider・lake hydrate |
| `stores/screening/runs.sqlite` | 再生成可能なScreening Run・Review Set | screening service |
| `stores/screening/calibration/current.sqlite` | 再生成可能なcalibration snapshot | calibration service |
| `stores/macro/macro.sqlite` | macro観測・vintage・取得情報。manual観測はGit seedから同期 | macro indicator service |

market storeをファイルごと消すとstore-local dataも失うため、lake所有tableが再生成可能という理由だけで削除しない。application DBのcloud copyはreplicaであり、localを上書きするための第二の正本ではない。

lakeの公開・hydrate・保持は[market lake](./reference/market-lake.md)、storeの転送・移行・復旧は[batch運用](../batch/OPERATIONS.md)が所有する。methodと表示configの入口は[method](../method/README.md)と[web/config](../web/config)を参照する。

<a id="cloud-serving-layer"></a>

## 4. 実行環境

判断と確認済み事実はlocalから書き込み、cloudへreplicaを公開する。cloudは日次の機械処理とservingを担う。

```text
local application DB ──publish──> R2 stores（application replica）
                                      ↑
GitHub Actions ──machine処理・転送──────┤
       └──read model生成──> R2 serving ──> Worker ──> browser
```

Owner MCPは所有者が保存済み情報を読むlocal adapterであり、store更新や定期batchを起動しない。toolと接続の契約は[Owner MCP](../tools/owner_mcp/README.md)を参照する。

<a id="repository-map"></a>

## 5. 機構と責務

| 配置・module | 責務 |
| --- | --- |
| `engine/src/baibai_engine/market` | L1の保持、固定release読取、market store |
| `engine/src/baibai_engine/macro` | 観測、Reading、Macro Context |
| `engine/src/baibai_engine/screening` | Security Analysis、Candidate Discovery、Triage、calibration |
| `engine/src/baibai_engine/research` | 企業評価、独立Review、CAA、Planning、Position Review |
| `engine/src/baibai_engine/position` | 確認済み取引事実、ledger replay、保有と資本の評価、outcome |
| `engine/src/baibai_engine/operation` | 資本調査のOperation Session |
| `engine/src/baibai_engine/tasks` | 運用task |
| `engine/src/baibai_engine/appdb` | application DB |
| `engine/src/baibai_engine/read_api` | engineの読み取りuse case |
| `web/` | read model、local UI、frontend、Worker、Web contract |
| `batch/` | 定期処理、local Triage runner、転送、通知 |
| `tools/` | 品質検査、実験、診断、所有者用adapter |
| `method/` | 採用した計算規則と調査playbook |
| `stores/` | 実行時store |
| `reports/` | historical evidenceと明示的なconsumer artifact |

### CLI

`baibai-engine`はdomainの読取・書込、`baibai-web`は表示・read model、`baibai-batch`はrepository内の運用処理の入口である。command・option・required・defaultはpublic `--help`を参照する。

### 投資判断と取引事実の依存

researchが企業評価と資本判断を所有し、core positionはresearchをimportしない。researchのbroker fact処理は保存済み判断のidentityを解決し、positionの事実記録へ渡す。

資本の現在値と未評価は`position/valuation.py`、quoteと権利basisの読取は`position/market_source.py`、ledgerの再構築は`position/ledger_read.py`に集約する。Planning、Position Review、read APIはこれらを使い、同じ算術・SQL・保存値検証を再実装しない。

<a id="failure-policy"></a>

## 6. Failure policy

無人経路が停止するのは、必須入力を得られない場合、または続行すると壊れたstore・release・viewを次の利用者へ渡す場合である。optionalな観測の不足・古さだけで無関係な出力まで止めず、対象値の未評価と理由を示す。

正常な未作成と保存物の破損を区別する。public readerの未作成file・tableを持たないunwritten storeは空viewとし、既存schemaの不一致やcurrent schemaのtable欠損は拒否する。書込可否を判断するreaderは必要なstoreの不在も拒否する。個別recordの隔離は出力契約を保てる場合に限り、必須の集合やledgerを勝手に欠落させない。

producerの計算identity変更は再生成の契機であり、それ自体を障害としない。入力世代の競合、履歴の後退、壊れた出力を防ぐ既存のschema・coverage・CAS等は維持する。batchの終了状態と公開・通知の成否は[batch運用](../batch/OPERATIONS.md)で扱う。

## 7. Development gates

型・保存・書込の条件はdomain model、DB constraint、serviceと対応testが所有する。検証層の使い分けと完全local gateは[Python foundation](./reference/python-foundation.md)を参照する。
