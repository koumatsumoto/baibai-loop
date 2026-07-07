---
name: tradingview-open
description: >-
  日本株の証券コードから TradingView のチャート URL を組み立て、Markdown リンクで併記しつつ
  WSL の既定ブラウザで開く。このリポジトリで銘柄（ticker / 証券コード）を提案・提示するときは
  毎回このリンクを必ず添える運用にしている。「<code> を TradingView で開いて」「チャート見せて」
  「銘柄のリンク出して」と言われたとき、および screening / 銘柄選定 / IR 調査など ticker を
  ユーザーに提示するすべての場面で使う。
---

# TradingView でチャートを開く（Baibai-Loop）

> **操作専用 skill**: docs 側に対応する仕様正本はなく、URL 組み立てと既定ブラウザ起動の〈操作〉だけを持つ。銘柄提示時に必ずリンクを添える運用ルールは [`operations/monthly-cycle.md`](../../../docs/operations/monthly-cycle.md) §5（取引提案）に従う。

ユーザーは普段 https://jp.tradingview.com/ の保存レイアウト（自分のインジケーター付きチャート）で銘柄を見ている。このリポジトリで銘柄を出すときは、その同じチャートへ即アクセスできるよう、TradingView URL を必ず併記し、既定ブラウザにも開く。

## URL 形式（canonical）

```
https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A<code>
```

- `<code>` は証券コード（例: `9715`。英数字コードの `130A` 等もそのまま入れる）。
- `fJupN99c` はユーザーの保存チャートレイアウト ID。固定で使うことで毎回ユーザーのインジケーター付きレイアウトで開く。`?symbol=` がレイアウト既定銘柄を上書きするので、どのコードでもこの 1 レイアウトで足りる。
- `:` は URL エンコードして `%3A`。symbol 部は必ず `TSE%3A<code>` の形にする。
- 取引所プレフィックスは東証銘柄なら `TSE`（このリポジトリの対象はほぼ全て東証）。非東証の稀なケースだけ調整する。

例（9715 トランスコスモス）:
`https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A9715`

## 提示の作法

銘柄を提案・提示するときは次の 2 つをセットで行う。リンク併記はクリックで開けるようにする保険、ブラウザ起動はユーザーが普段の確認動作を省ける利便。

1. **Markdown リンクを併記**: コード + 社名をリンクテキストにする。
   `[9715 トランスコスモス](https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A9715)`
2. **既定ブラウザで開く**: 下のコマンドで開く。

## 開き方（WSL / Windows）

対応環境は WSL (Ubuntu) と Windows (Git Bash) のみ。既定ブラウザで URL を開くには PowerShell の `Start-Process` を使う。`<code>` を実コードに置き換えて実行する。

```bash
powershell.exe -NoProfile -Command "Start-Process 'https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A<code>'"
```

- `explorer.exe "<url>"` は使わない。`?` や `%3A` を含む query 付き URL を URL として解釈できず、ファイルマネージャのホームが開いてしまう。
- `cmd.exe /c start "<url>"` も使わない。cmd が `%3A` の `%3` を batch 変数として展開して URL を壊す。
- PowerShell の単一引用符は `%3A` をそのまま保持し、`Start-Process` が URL の既定ハンドラ（＝既定ブラウザ）を起動する。失敗時は非 0 を返すので終了コードで判断できる。

複数銘柄を開くときは銘柄ごとに 1 コマンドで開く（その数だけタブが開く）。

## 大量提示のときの加減

screening の TOP12 のように一度に多数の ticker を出す場面で全部を自動で開くと、タブが溢れて逆に邪魔になる。意図は「ユーザーが見たい銘柄をすぐ確認できる」ことなので、次のように加減する。

- ユーザーが実際に検討する **focus 銘柄（最終候補・推し 1〜数件）は自動で開く** + リンク併記。
- 大量の一覧（候補テーブル等）は **リンク併記は全件**、ブラウザ起動は focus 分だけにして「残りも開く?」と一言添える。

リンク併記自体は件数に関わらず常に行う（コストはほぼゼロで、ユーザーがクリックで任意に開ける）。

## 対応外環境

WSL / Windows 以外（素の Linux / macOS など）ではブラウザ起動はせず、Markdown リンク併記だけ行い、自動では開けない旨を伝えて止まる。
