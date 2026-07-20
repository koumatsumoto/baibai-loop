# Macro indicators 長期文脈・表示粒度 運用テスト（2026-07-20）

## 目的と採用条件

macro indicators の取得 cache と cockpit を、金利・為替・インフレ・景気・市場ストレスを景気サイクル単位で読める状態に保つ。macro は判断入力であり、screening・ranking・sizing の機械経路へ接続しない。

採用条件は次のとおりである。

- FRED 全系列の DB 先頭観測日が、現在の `fredgraph.csv` の先頭行と一致する。
- JP 系列は一次 provider と利用契約が返す最古範囲を記録できる。
- 読み出し時の週次・月次・年次集約が期間最終値を返し、期間と粒度を cockpit 全チャートへ適用できる。
- symbol を定義できる系列だけ TradingView へ安全な新規 tab で遷移する。
- 全履歴を保持する SQLite の実サイズと最大 API 応答を運用可能な範囲として確認できる。

## 系列選定

| series_id | 判断入力 | source | 単位・頻度 | 取得範囲 |
| --- | --- | --- | --- | --- |
| `jp.real_effective_exchange_rate` | 相対物価を含む円の購買力・競争力 | BIS broad REER / FRED `RBJPBIS` | 2020=100・月次 | 1994-01-01〜2026-05-01、389点 |
| `jp.hourly_earnings` | 家計所得と製造業人件費圧力 | OECD MEI / FRED `LCEAMN01JPM661S` | 2015=100・月次・季調済 | 1955-01-01〜2026-04-01、856点 |
| `jp.cpi.services` | 国内サービス価格の粘着性 | 総務省 CPI / e-Stat `0003427113`、全国・サービス・指数 | 指数・月次 | 1970-01-01〜2026-05-01、677点 |

選定根拠と取得品質の判断は [issue #465 comment](https://github.com/koumatsumoto/baibai-loop/issues/465#issuecomment-5018978051) に置く。日本株 breadth、日本株 valuation / ERP、provider 公表カレンダーは安定した一次取得契約または数値 observation schema への適合を確認できず、実装可否の人間判断対象である。

## 全履歴 backfill

実データ `data/indicators/macro.sqlite` の clone に、全非 manual provider の強制取得と manual seed import を適用する。取得前は 55系列・45,324観測・15,831,040 bytes、取得後は58系列・269,931系列日・112,963,584 bytes（`du -h`: 108MiB）である。vintage 行と読取索引は provider revision と集約性能に使うため保持する。

FRED 35系列は現在の `fredgraph.csv` を個別取得し、全系列で `MIN(observed_at)` を先頭行と機械照合する。結果は `series=35 / mismatches=0` である。ICE BofA OAS 3系列はFREDの現在の配信範囲が直近3年、S&P 500 / Dow は直近10年であり、現在のCSV先頭日を再現可能な境界とする。

| provider | 系列数 | clone の取得範囲 | distinct points | 制約 |
| --- | ---: | --- | ---: | --- |
| `frb_h15` | 3 | 1962-01-02〜2026-07-16 | 41,173 | H.15 package に存在する系列開始日まで |
| `ecb_fx` | 3 | 1999-01-04〜2026-07-17 | 21,153 | ECB euro reference rates の開始日まで |
| `yahoo` | 6 | 1987-09-10〜2026-07-20 | 43,234 | symbol ごとの提供開始日に依存 |
| `multpl` | 3 | 2026-06-27〜2026-07-20 | 11 | provider は current level だけを返す |
| `estat` | 2 | 1970-01-01〜2026-05-01 | 1,354 | CPI 現行表の月次範囲 |
| `boj` | 1 | 1970-01-01〜2026-06-01 | 678 | 長期時系列 workbook の収録範囲 |
| `boj_mutan` | 1 | 2025-01-06〜2026-07-16 | 378 | 日別 file 巡回を直近2暦年に限定 |
| `mof_jgb` | 1 | 1986-07-05〜2026-07-16 | 9,903 | 財務省全履歴 CSV の10年列開始日まで |
| `jquants_flows` | 1 | 2022-04-14〜2026-07-16 | 224 | 利用契約は2021-07-20以後、TSE Prime データは2022-04-14から存在 |
| `manual` | 2 | 2026-01-01〜2026-04-01 | 8 | git 管理 seed 12 observation を `import-manual` で同期 |

## API と cockpit

実データ clone を read-only API で読み、次を計測する。

| period / granularity | groups / series | points | response bytes | elapsed |
| --- | --- | ---: | ---: | ---: |
| `1y / daily` | 3 / 8 | 1,510 | 66,635 | 37.8ms |
| `5y / weekly` | 3 / 8 | 1,343 | 59,608 | 22.0ms |
| `10y / monthly` | 3 / 8 | 662 | 30,064 | 30.6ms |
| `max / yearly` | 3 / 8 | 276 | 13,302 | 88.6ms |
| `max / daily` | 3 / 8 | 54,080 | 2,357,012 | 144.0ms |

未知の `period` と `granularity` はともに HTTP 422 を返す。初期表示は3グループ・8系列、`1y / daily` である。Chrome の実画面で `10年 / 月次` を選ぶと、全チャート用に `/api/macro?period=10y&granularity=monthly` を再取得する。

初期8カードでは TradingView symbol 定義済みの日本10年国債、米国10年国債、EUR/JPY、日経平均にアイコンを表示する。各 link は `https://www.tradingview.com/chart/?symbol=...`、`target=_blank`、`rel="noopener noreferrer"` を持つ。symbol 未定義の4カードには link がない。registry 全体では19系列に symbol を定義する。

## 採用判定と監視

長期履歴、read-time 集約、期間・粒度切替、TradingView 導線を採用する。`max / daily` もローカル read model の体感待ちを生まない応答範囲であり、集約 table は持たない。

継続確認は次に限定する。

- FRED の配信境界が動く系列は `refresh --all-history` 後に先頭日照合を行う。
- JP provider は契約・archive の最古日を運用記録へ残す。
- TradingView symbol drift はカード導線の目視と registry test で検出する。
- macro DB は取得 cache として扱い、manual seed と series registry から再構築可能な状態を保つ。
