# EDINET CapEx抽出fixture

EDINET API v2の`documents/{doc_id}?type=5`から2026-09-17に取得したCSV-ZIPのうち、既存財務抽出対象conceptの行をTSVへ抜粋したもの。行のconcept/context・連結区分・単位・値は原本のまま保持する。

| doc_id | ticker | 原本ZIP SHA256 |
| --- | --- | --- |
| S100YU8C | 5946 | cbaed23e6b6b4dea610ca8773065af9b178b8d6db063d52e791a07aad6fbdc46 |
| S100YI50 | 7122 | e0b9518a3d8dfdd597e6b46f3881bb0bf1ff61e7002f71af90bbae9ce07db9a6 |
| S100YIDK | 2415 | 7a66793cc903cd427d1a24201a8de4ced4a8fdf317f4323836fb69ee725c30f7 |
| S100YK4M | 4231 | e6c957454c1a549149bd686e584b2b5203b19ba47a404b229b46927235b2d92e |
| S100YK21 | 1814 | 13bae46d1ebeacbbdd7b51ad355da2042ead8f970d5e4dfbf89601011d7069f9 |
| S100YPE4 | 7991 | 7e51fecfd1a5b2c7193c0f93e1f6b5071810a90d22b4e3850c5788a0646c0798 |
| S100XXXU | 8127 | be3c8bcf178bc6974beea1bd968a749786dc312dd981d24048e3a6dad24f87cb |

S100YU8Cの`InterimDuration`は2026-01-01〜06-30。連結・JPYのCFO4,857百万円、合算CapEx△1,136百万円は[会社半期報告書](https://www.chofu.co.jp/detail_news.php?id=500)と[短信](https://www.chofu.co.jp/detail_news.php?id=499)の当中間期CFに一致する。APIの書類期末2026-12-31をこの6か月フローの終点に使わない。

## 標準IFRSの原本抜粋（#1556）

S100YV6Y（INPEX半期）・S100YTCC（JT半期）のCSVは2026-10-18保存済み原本から、S100XU9L（INPEX年次）のCSVは2026-10-20 23:33 UTCにEDINET API v2で取得した原本から抜粋。元の行・context・単位・値を保ち、当期だけでなく前期・個別・セグメントcontextも含める。以下はCSV ZIPのSHA256。全文原本ではない。

- S100YV6Y: `5ee8099cf5109a7ca9972fe97956c4c4182943a2c8fc7c312a5805179576b6f0`
- S100YTCC: `35e43cfbdf31e855d6f7ecdf4435e9745540acbe42bbd3518bdd190029d7ea43`
- S100XU9L: `886881db589ca476a39bdbe300b00c7e38ff4e9c2e934562a0d94bd2f632eac0`

対応するtype=1 XBRLのcontextも確認。前二書類のInterimDurationは2026-01-01〜06-30、InterimInstantは06-30。年次は2025-01-01〜12-31と12-31。半期書類metadataの12-31は半期CFの終点ではなく、抽出値を年率化しない。JTの有形/無形取得は両方存在するが、INPEXの探鉱評価・開発生産資産取得は会社独自科目なので、標準有形取得だけから全CapEx/FCFを作らない。債券・借入金だけを完全な金融債務にしない。
