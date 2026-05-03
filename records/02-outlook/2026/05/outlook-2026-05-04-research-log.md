# Outlook 2026-05-04 Research Log

`outlook-2026-05-04-post-fomc-boj-hold.yaml` 作成時に取得した一次情報源 (Tier 1 / Tier 1 準拠) のカタログ。outlook 本体の `summary` で参照される具体的な fact + URL を保存する。outlook YAML 自体には schema 都合で URL 一覧を入れにくいため、sidecar として分離する。

取得日: 2026-05-04
取得方法: deep research (general-purpose subagent + WebFetch)

## Axis 1: 米マクロ追加 fact

1. **BEA - Personal Income and Outlays (2026-03)**
   - URL: https://www.bea.gov/news/2026/personal-income-and-outlays-march-2026
   - Tier: 1 (BEA、米商務省)
   - Key fact: 2026-03 PCE 価格指数 +3.5% YoY、コア PCE +3.2% YoY、個人所得 +0.6% MoM (+1,492 億 USD)、PCE +0.9% MoM (+1,954 億 USD)。2026-04-30 公表
   - 含意: コア PCE 3.2% は Fed 目標 2% を大きく上回り、エネルギー除いても基調インフレ粘着、FOMC タカ派ホールドの正当化材料

2. **U.S. Census Bureau - Advance Monthly Retail Trade Survey (2026-03)**
   - URL: https://www.census.gov/retail/marts/www/marts_current.pdf
   - Tier: 1 (米商務省 Census)
   - Key fact: 2026-03 小売売上高 7,521 億 USD、+1.7% MoM、+4.0% YoY。Nonstore +10.1% YoY。Q1 累計 +3.7% YoY。ガソリン部門は +15.5% MoM、ガソリン除き retail trade も +0.6% MoM
   - 含意: 額面強い。ガソリンが headline を押し上げているが、ガソリン除きでも +0.6% MoM で消費は底堅く、実質評価は real PCE / control group の確認待ち

3. **BLS Schedule (米雇用 4 月分)**
   - URL: https://www.bls.gov/schedule/news_release/empsit.htm
   - Tier: 1 (BLS)
   - Key fact: 米 2026-04 雇用統計は 5/8 (金) 8:30 ET 発表予定。直近 3 月 NFP +178k、失業率 4.3%

4. **BLS Schedule (米 CPI 4 月分)**
   - URL: https://www.bls.gov/schedule/news_release/cpi.htm
   - Tier: 1 (BLS)
   - Key fact: 米 4 月 CPI は 5/12 (火) 8:30 ET 発表予定。直近 3 月 +3.3% YoY、コア +2.6%、ヘッドライン +0.9% MoM

5. **U.S. Census Bureau / HUD - New Residential Construction (2026-03)**
   - URL: https://www.census.gov/construction/nrc/index.html
   - Tier: 1 (米商務省 Census)
   - Key fact: 2026-03 一戸建て住宅着工 SAAR 94 万戸、-14.2% MoM (コロナ初期以来の大幅減)、許可も 4 ヶ月ぶり低水準
   - 含意: 高金利 + 中東リスクで建設サイクルが折れる兆候、Fed 利下げ遅れの impact

6. **White House Proclamation 11012 / Federal Register (Section 122 関税)**
   - URL: https://www.federalregister.gov/documents/full_text/html/2026/02/25/2026-03824.html
   - Tier: 1 (Federal Register、White House Proclamation)
   - Key fact: Section 122 of the Trade Act of 1974 に基づく 10% ad valorem temporary import surcharge を 2026-02-24 12:01 EST から適用、150 日間 (2026-07-24 12:01 EDT 期限)。USMCA goods・critical minerals・energy resources・agricultural・pharmaceuticals・vehicles・aerospace 等 (Annex I/II 列挙) は除外
   - 注意: 「13% 上乗せ」は外部分析の trade-weighted estimate であり、Federal Register 本文の率は 10%。outlook で 13% を引用する場合は外部 estimate と明記する必要がある

## Axis 2: 地政学・エネルギー

7. **OPEC JMMC Press Release (2026-04-05)**
   - URL: https://www.opec.org/pr-detail/1756597-5-april-2026.html
   - Tier: 1 (OPEC)
   - Key fact: 8 ヶ国の自主削減 1.65mb/d のうち 206kb/d 分の調整を 2026 年 5 月から実施。次回会合 2026-05-03

8. **EIA Short-Term Energy Outlook April 2026**
   - URL: https://www.eia.gov/outlooks/steo/pdf/steo_full.pdf
   - Tier: 1 (EIA)
   - Key fact: 2026 年 Brent 平均 $96/b (前月想定 $79 から +22% 上方修正)、Q2 ピーク $115/b、Q4 $88/b。WTI Q1 $72.74 → Q2 $101.63 → Q4 $59.64。米原油生産 13.5mb/d (2026)
   - 含意: 原油は Q2 ピーク後減衰想定、コスト押上げは Q2-Q3 がピーク

9. **EIA Press Release - Hormuz closure (2026-04-07)**
   - URL: https://www.eia.gov/pressroom/releases/press586.php
   - Tier: 1 (EIA)
   - Key fact: 中東 6 ヶ国 (イラク、サウジ、クウェート、UAE、カタール、バーレーン) が 2026-03 7.5mb/d、2026-04 9.1mb/d 生産シャットイン。ホルムズ海峡実質閉鎖 2026-02-28 以降。世界石油在庫 3 月 -85m bbl
   - 含意: 全世界石油供給 20% のチョークポイント停止、価格急騰の核心

10. **IEA Oil Market Report April 2026**
    - URL: https://www.iea.org/reports/oil-market-report-april-2026
    - Tier: 1 (IEA)
    - Key fact: 2026 年通年需要 -80kb/d (前回 +730kb/d から下方修正)、Q2 -1.5mb/d はコロナ以来の急減。アジア石油化学・LPG・ジェット燃料中心に減退
    - 含意: 価格急騰で需要破壊が顕在化、スタグフレーションリスクの一次根拠

11. **IEA Strait of Hormuz Briefing**
    - URL: https://www.iea.org/about/oil-security-and-emergency-response/strait-of-hormuz
    - Tier: 1 (IEA)
    - Key fact: ホルムズ海峡通過の原油・コンデンセート約 20mb/d (世界海上原油 30%、世界石油消費 20%)。代替パイプライン余力限定的

## Axis 3: 為替・金融政策

12. **Federal Reserve - FOMC Statement 2026-04-29**
    - URL: https://www.federalreserve.gov/newsevents/pressreleases/monetary20260429a.htm
    - Tier: 1 (FRB)
    - Key fact: FF 金利 3.50-3.75% 据置、8-4 vote (Miran 利下げ希望、Hammack/Kashkari/Logan は hold だが easing bias 削除)。声明文「Inflation is elevated, in part reflecting the recent increase in global energy prices」

13. **日本銀行 - 経済・物価情勢の展望 (2026-04 基本的見解)**
    - URL: https://www.boj.or.jp/mopo/outlook/gor2604a.pdf
    - Tier: 1 (日本銀行)
    - Key fact: 2026 年度コア CPI 見通しを +2.8% に上方修正 (1 月 +1.9% から +0.9pt)。2027 年度 +2% 台前半、2028 年度 +2% 程度。経済下振れ、物価上振れリスク強調

14. **日本銀行 - 総裁定例記者会見 (2026-04-30 公表)**
    - URL: https://www.boj.or.jp/about/press/kaiken_2026/kk260430a.pdf
    - Tier: 1 (日本銀行)
    - Key fact: 4/27-28 会合 6-3 ホールド (3 名利上げ反対)。中東情勢の不確実性、エネルギー価格動向、利上げ時期に言及

15. **ECB - Monetary Policy Decisions (2026-04-30)**
    - URL: https://www.ecb.europa.eu/press/pr/date/2026/html/ecb.mp260430~81b7179e6f.en.html
    - Tier: 1 (ECB)
    - Key fact: 預金 2.00% / MRO 2.15% / 限界貸付 2.40% 据置。ユーロ圏 4 月フラッシュ HICP +3.0%。「upside risks to inflation and downside risks to growth have intensified」

16. **Bank of England - April 2026 MPC Summary & Minutes**
    - URL: https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2026/april-2026
    - Tier: 1 (BOE)
    - Key fact: Bank Rate 3.75% を 8-1 で据置。Pill が 4.00% 利上げ票。英国 3 月 CPI +3.3%

17. **Bank of England - Monetary Policy Report April 2026**
    - URL: https://www.bankofengland.co.uk/-/media/boe/files/monetary-policy-report/2026/april/monetary-policy-report-april-2026.pdf
    - Tier: 1 (BOE)
    - Key fact: 中東紛争でグローバルエネルギー見通し不確実化、ガソリン価格上昇が家計直撃、second-round effects に注意

## Axis 4: 日本マクロ

18. **連合 - 2026 春季生活闘争 第 3 回回答集計 (2026-04-03)**
    - URL: https://www.jtuc-rengo.or.jp/activity/roudou/shuntou/index2026.html
    - Tier: 1 (連合)
    - Key fact: 賃上げ率 5.09% (前年同期比 -0.33pt)、中小組合 5.00%、有期・短時間 +6.89%。3 年連続 5% 超

19. **日本銀行 - 短観 2026-03 (要旨)**
    - URL: https://www.boj.or.jp/statistics/tk/yoshi/tk2603.htm
    - Tier: 1 (BOJ)
    - Key fact: 大企業製造業 DI +17 (前回比 +1pt)、大企業非製造業 +36。2025 年度設備投資計画 全規模全産業 +7.9%、大企業製造業 +12.3%。2026 年度初回計画 +1.3%

20. **経済産業省 - 鉱工業生産指数 2026-03 速報**
    - URL: https://www.meti.go.jp/statistics/tyo/iip/result/book/b2020_202603sj.html
    - Tier: 1 (METI)
    - Key fact: 前月比 -0.5%、出荷 -1.1%。製造工業生産予測指数 4 月 +2.1%、5 月 +2.2%。電子部品・デバイス、生産用機械が底堅い

21. **財務省 - 貿易統計 2026-03**
    - URL: https://www.customs.go.jp/toukei/shinbun/happyou.htm
    - Tier: 1 (財務省)
    - Key fact: 輸出 +11.7% YoY (7 ヶ月連続増)、輸入 +10.9%、貿易収支 +6,670 億円。半導体製造装置・IC 増加、対中東向けはホルムズ封鎖で減少、自動車減少

22. **内閣府 - 景気ウォッチャー調査 2026-03**
    - URL: https://www5.cao.go.jp/keizai3/2026/0408watcher/menu.html
    - Tier: 1 (内閣府)
    - Key fact: 現状判断 DI (季調) 42.2、前月比 -6.7pt 急冷、先行きも大幅悪化、中東情勢緊迫を反映

23. **総務省 - 東京都区部 CPI 2026-04 (中旬速報値)**
    - URL: https://www.soumu.go.jp/menu_news/s-news/01toukei08_01000342.html
    - Tier: 1 (総務省)
    - Key fact: 4 月東京都区部 生鮮除き総合 +1.5% YoY (前月比 +0.1pt)、3 ヶ月連続 2% 割れ、諸雑費 -7.1% YoY 押し下げ
    - 含意: 全国 CPI 先行指標、エネルギー高でも日本コア CPI 鈍化方向、日銀利上げハードル意外に高い

24. **日本銀行 - 消費活動指数**
    - URL: https://www.boj.or.jp/research/research_data/cai/index.htm
    - Tier: 1 (BOJ)
    - Key fact: 個人消費の月次・四半期動向把握用、家計調査・商業動態より速報性高い

## Axis 5: セクター動向

25. **TSMC - 2026 Q1 Earnings Release**
    - URL: https://investor.tsmc.com/english/encrypt/files/encrypt_file/reports/2026-04/e85216eea8dccd8ca75d7e040e8d57be3ccd618b/1Q26%20EarningsRelease.pdf
    - Tier: 1 (TSMC IR)
    - Key fact (Q1 release で確認できた範囲): Q1 2026 売上 359 億 USD、グロスマージン 66.2%、オペマージン 58.1%
    - **注意**: 「Capex $52-56B レンジ上限」「Capex 70-80% 先端プロセス配分」「2026 年売上 +30%」は Q1 release 本文だけでは確認できず、**Q4 (2025) earnings transcript / IR ガイダンス由来の値を引用している**。引用粒度を上げるなら TSMC IR Archive の Q4 transcript を別 source として明示する必要

26. **NVIDIA - Q4 / Fiscal 2026 Financial Results (2026-02-25)**
    - URL: https://nvidianews.nvidia.com/news/nvidia-announces-financial-results-for-fourth-quarter-and-fiscal-2026
    - Tier: 1 (NVIDIA IR)
    - Key fact (press release で確認できた範囲): FY2026 通年売上 2,159 億 USD (+65% YoY)、データセンター 1,973 億 USD、Q4 売上 681 億 USD (+73%)、データセンター 623 億 USD、Q1 FY27 ガイ 780 億 USD (±2%)
    - **注意**: 「Sovereign AI 売上 300 億 USD 超」「供給コミットメント Q3 末 503 億 → Q4 末 952 億 USD」は press release だけでは確認できず、**earnings call transcript / 10-K / 10-Q の本文確認が必要**。本 outlook では参考値扱いで、確度の高い fact としては FY26 通年売上と Q1 FY27 ガイダンスのみを使う

27. **Baltic Exchange - Baltic Dry Index (April 2026)**
    - URL: https://www.balticexchange.com/en/data-services/market-information0/dry-services.html
    - Tier: 1 (Baltic Exchange)
    - Key fact: BDI 4 月中旬 2,484 (+5.5%)、4/24 時点 2,665、4/29 時点 2,670、Capesize 主導で 4 ヶ月ぶり高水準

28. **JAMA - 統計月報**
    - URL: https://www.jama.or.jp/statistics/m_report/index.html
    - Tier: 1 (JAMA)
    - Key fact: 月次の四輪車生産・販売・輸出統計、財務省貿易で対中東向け減少 (ホルムズ)

29. **METI - 鉱工業指数 (生産能力・稼働率)**
    - URL: https://www.meti.go.jp/statistics/tyo/iip/index.html
    - Tier: 1 (METI)
    - Key fact: 3 月分で輸送機械 (除く自動車)、生産用機械、電子部品・デバイスが上昇、化学・汎用機械・石油石炭が低下

## Axis 6: リスク資産・債券

30. **FRED - ICE BofA US Corporate Index OAS (BAMLC0A0CM)**
    - URL: https://fred.stlouisfed.org/series/BAMLC0A0CM
    - Tier: 1 (FRED / ICE BofA)
    - Key fact: 米 IG OAS 4 月初時点 約 80bp、25 年来歴史的タイト。BBB 100bp、AA 51bp、AAA 40bp。長期平均 ~150bp 大きく下回る
    - 含意: クレジットはストレス未織込み、調整余地

31. **FRED - ICE BofA US High Yield Index OAS (BAMLH0A0HYM2)**
    - URL: https://fred.stlouisfed.org/series/BAMLH0A0HYM2
    - Tier: 1 (FRED / ICE BofA)
    - Key fact: 米 HY OAS 4 月 約 283bp、Q1 末も 285bp 近辺、歴史的タイト

32. **U.S. Treasury - Daily Treasury Par Yield Curve Rates**
    - URL: https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve&field_tdr_date_value=2026
    - Tier: 1 (米財務省)
    - Key fact: 2026-04-10 10Y 4.31% / 2Y 3.81% (10s2s +50bp)、4 月末 10Y 4.45% 試した後 4.40%、5/1 4.35%

33. **CBO - The Budget and Economic Outlook: 2026 to 2036**
    - URL: https://www.cbo.gov/publication/61882
    - Tier: 1 (CBO)
    - Key fact: FY2026 連邦財政赤字 1.9 兆 USD (GDP 比 5.8%)、2036 年 6.7%。連邦債務 2036 年 GDP 比 120%、2056 年 175%。利払い 2026 年 1.0 兆 USD (3.3% GDP) → 2036 年 2.1 兆 USD (4.6%)

34. **FRED - CBOE Volatility Index (VIXCLS)**
    - URL: https://fred.stlouisfed.org/series/VIXCLS
    - Tier: 1 (FRED / CBOE)
    - Key fact: 4 月平均 16.89、3/9 ピーク 35.30 から急低下、4/2 close 23.87、4/25 close 18.71

## Axis 7: 中国・新興国

35. **国家統計局 (NBS) - 2026-04 中国採購経理指数**
    - URL: https://www.stats.gov.cn/sj/zxfb/202604/t20260430_1963473.html
    - Tier: 1 (中国 NBS)
    - Key fact: 4 月製造業 PMI 50.3 (前月比 -0.1pt)、生産 51.5、新規受注 50.6、大企業 50.2、中小 50.5/50.1。総合 PMI 50.1 (-0.4pt)。2 ヶ月連続拡張圏

36. **海関総署 - 2026-03 中国輸出統計**
    - URL: http://www.customs.gov.cn/
    - Tier: 1 (中国海関総署)
    - Key fact: 2026-03 輸出 (USD ベース) +2.5% YoY、エコノミスト予想 +8.6% を大幅下振れ。対米輸出 -26% 超。前月 (2 月) は +40% 近かった
    - 含意: 122 条関税効果が顕在化、対米輸出失速

37. **NBS - 中国不動産投資 (Q1 2026)**
    - URL: https://www.stats.gov.cn/
    - Tier: 1 (NBS)
    - Key fact: 2026 Q1 累計 不動産投資 -11.2% YoY、住宅販売 -18.5% YoY。2026 GDP 成長率 4.5-5% 目標の下限近辺見通し

## 取得不能だった軸 (今後の改善対象)

- 米シェール盆地別週次データ (EIA Drilling Productivity Report が次の参照先)
- 新興国通貨 / EM ETF flow (IIF / EPFR は無料分の速報性に欠ける)
- 春闘最終結果 (連合最終第 5 回集計は 7 月公表予定)
- 日銀 4 月会合「主な意見」(2026-05-12 公表予定)
- ロシア・ウクライナ / サウジ・イラン関係の一次外交文書 (国務省 / 外務省)
- 日本 法人企業統計 2025 年度 Q4 (1-3 月、6 月初公表予定)
- SCFI コンテナ運賃 (上海航運交易所サイト直接取得不可、Drewry WCI が代替)

## 要約 (5 行、日本株マクロ含意)

1. 中東 × ホルムズ封鎖 (2/28 以降) がコモディティ・物価・需要を同時に揺さぶり、Fed・ECB・BOE・日銀全員が「ホールド + データ次第」で身動き取れず
2. EIA は Brent $96/b (2026 年通年)、Q2 ピーク $115/b、IEA は需要を年 -80kb/d 下方修正 → スタグフレーション正面化
3. 米クレジット (IG 80bp / HY 283bp) は歴史的タイトでショック未織込み、先行き調整リスク残存
4. 日本側は短観製造業 +17、設備投資 +7.9% で底堅いが、東京 CPI +1.5% / 景気ウォッチャー -6.7pt と内需冷却。日銀は利上げハードル上昇で 6 月以降に持越し
5. AI 関連 (TSMC Capex 上方、NVIDIA FY27 Q1 ガイ 780 億 USD) は揺るぎなく、日本の半導体製造装置・素材・電力関連が macro outlook の構造的下支え
