# stores/market

`market.sqlite` は価格・calendar・開示データを保持する rebuildable L1 store です。
cloud の日次更新と local の深い履歴を merge して authority を維持します。

`projection.sqlite` は固定した L1 release から作る使い捨ての射影で、authority ではありません。
削除しても `baibai-engine lake projection build` で release から再構築でき、R2 へ upload しません
（[market lake](../../docs/reference/market-lake.md)）。
