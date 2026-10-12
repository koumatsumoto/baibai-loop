---
title: "External Assessmentとstorageの構造移設比較"
summary: "tracked file対応とEDINET抽出意味の旧新比較。既存保存値を変更しない構造整理。"
doc_type: report
status: historical
---

# 構造移設比較

基準commit `cc5afa9a629ad6011369b80bb1905ccd4ccd7faa` の全tracked 1,020ファイルを `file-map.csv` に記録する。move 99・update 208・unchanged 713と最終path/owner/理由を照合した。#1426・#1428でmainへ入ったResearch/Triageの変更と4件の所有移設も最終pathへ統合しているため、この比較表全体を#1427単独の変更数とは扱わない。追加fileはPR差分で確認する。後続のValuation/Market Context整理を完了扱いにしない。

実DB/table/dataset/raw prefix/plan形式とworkflow登録名・concurrencyは保持する。旧packageのimport再exportは作らない。実データ取得・書込・移動・削除は行っていない。

保存readerはpure Market modelsとtechnical helperを参照し、provider取得実装・EDINET domain store・SQLite writerをcold importしない。EDINET開示型、JPXの型/例外とJ-Quants decoderをpure Marketへ分離し、callerは保存leafへ直接追従した。foundationとValuationの全storage依存禁止、Web/batchの直接storage依存禁止、共有storage・TradingView contract・Candidate/Triage contractの依存規則を明示した。

## EDINET抽出意味の比較

旧実装 `548373b8053beadd9f4a3c8dee2ef10eb6338a15` が生成するtokenは `9b82e8d0d13ac87aaf325561de0d93fb54466178254f49856beff1b433e07187`。配置変更前後の文書選択・抽出・正規化・保存変換にある96定義を一回限りで照合し、importを除く実装差分はなかった。恒久のAST更新検知器は追加していない。

同じsynthetic原CSV ZIPと書類一覧を旧新の隔離processで処理し、次の10ケースについて文書選択、保存38列、quality/failure理由、coverage/count、exit code、取得呼出しを比較した。全結果が一致し、比較JSONの旧新SHA-256はともに `eb5ec10833026b0f85fe0cf4384fe63f716fb9d218ddfb2b093bd6c9ff64490c` だった。実providerは呼ばず、既存canonicalは変更していない。

|ケース|2銘柄の2回目処理|
|---|---|
|正常・quality欠測・0とNULL|再取得0。保存値と理由を再利用|
|文書編集|編集された1書類を再抽出|
|取下げ|対象を隔離し、正常書類は再利用。再取得0|
|別revision・NULL revision・failed coverage|2書類を再抽出|
|hard parser failure・破損count|exit 1、再取得0、新snapshotを書かない|

この意味同一を根拠に、全source bytes/import closureからのhash導出を削除し、明示的な`EDINET_METRIC_REVISION`へ旧tokenを継承した。保存済み行・値・日時・旧release/hashは書き換えない。専用AST/manifest検査と配置だけのcutover手順を削除し、文書identity・quarantine・hard failure・coverage/countの実用回帰は維持する。意味revisionの更新範囲と公開commitのprovenanceは[architecture](../../../docs/architecture.md#edinet-metric-revision)に集約した。

#1415の差分はtransport retryの既存3methodと新しいretry helperに限定され、抽出値・文書選択・正規化の定義は変えない。retry回数/待機だけでは意味revisionを更新しない。#1415自体の採否・統合は別PRで扱う。
