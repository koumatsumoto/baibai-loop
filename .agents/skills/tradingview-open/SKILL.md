---
name: tradingview-open
description: Baibai-Loopでtickerを提示し、TradingView linkを付け、focus銘柄のchartをWSL/Windowsの既定browserで開くときに使う。
---

# TradingView Open

- URLは`https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A<code>`を使う。
- 提示する全tickerへMarkdown linkを付ける。
- focus tickerだけ`powershell.exe -NoProfile -Command "Start-Process '<url>'"`で開く。
- queryを壊す`explorer.exe`と`cmd start`を使わない。
- screening大量一覧は全件link、browser openはfocusだけにする。
- 投資判断、IR、ranking、指値の規範をこのskillへ持たない。
