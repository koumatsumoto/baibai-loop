# EDINET CapEx抽出fixture

EDINET API v2の`documents/{doc_id}?type=5`から2026-09-17に取得したCSV-ZIPのうち、既存財務抽出対象conceptの行をTSVへ抜粋したもの。行のconcept/context・連結区分・単位・値は原本のまま保持する。

| doc_id | ticker | 原本ZIP SHA256 |
| --- | --- | --- |
| S100YU8C | 5946 | cbaed23e6b6b4dea610ca8773065af9b178b8d6db063d52e791a07aad6fbdc46 |
| S100YI50 | 7122 | e0b9518a3d8dfdd597e6b46f3881bb0bf1ff61e7002f71af90bbae9ce07db9a6 |
| S100XXXU | 8127 | be3c8bcf178bc6974beea1bd968a749786dc312dd981d24048e3a6dad24f87cb |

S100YU8Cの`InterimDuration`は2026-01-01〜06-30。連結・JPYのCFO4,857百万円、合算CapEx△1,136百万円は[会社半期報告書](https://www.chofu.co.jp/detail_news.php?id=500)と[短信](https://www.chofu.co.jp/detail_news.php?id=499)の当中間期CFに一致する。APIの書類期末2026-12-31をこの6か月フローの終点に使わない。
