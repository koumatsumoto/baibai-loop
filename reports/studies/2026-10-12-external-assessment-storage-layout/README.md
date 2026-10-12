---
title: "External Assessmentとstorageの構造移設比較"
summary: "親決定に沿う一回限りのtracked file対応表。永続identity切替前の段階。"
doc_type: report
status: historical
---

# 構造移設比較

基準commit `cc5afa9a629ad6011369b80bb1905ccd4ccd7faa` の全tracked 1,020ファイルを `file-map.csv` に記録する。move/update/unchanged、最終path、owner、理由は今回の構造移設の範囲であり、親による後続のValuation/Market Context/Research整理を完了扱いにしない。追加されたidentity境界とpackage init、移設回帰テストはPR差分で照合する。

実DB/table/dataset/raw prefix/plan形式とworkflow登録名・concurrencyは保持する。旧packageのimport再exportは作らない。EDINET抽出revisionはsource path/bytesの移動で変化するため、計算値を変えないprovenance切替を後段の保存移行で扱う。実データ取得・書込・移動・削除は行っていない。


最終比較はmove 95・update 159・unchanged 766。追加fileは純粋JPX models、J-Quants coverage contract、identity境界、package init、移設回帰テストと本報告であり、CSVは基準tracked fileの対応を一回限りで記録する。

保存readerはpure Market modelsとtechnical helperを参照し、provider取得実装・EDINET domain store・SQLite writerをcold importしない。JPXの型/例外とJ-Quants decoderをpure Marketへ分離し、callerは保存leafへ直接追従した。foundationとValuationの全storage依存禁止、Web/batchの直接storage依存禁止、共有storageとTradingView contractの依存規則を明示した。
