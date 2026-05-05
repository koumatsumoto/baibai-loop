# records/_external/

外部 AI / 二次分析の **生原稿保管領域**。`records/04-research/` に取り込む前段の素材として、
他 AI セッション、証券レポート、アナリストノート、ニュース要約などをそのまま残す。

## 1. 目的

- 「概ね正しい / 修正 / 未採用」の検証ログだけでは、**元出力との差分**が後から復元できない
- 採用 / 修正 / 未採用の整合を機械的に check するには、生原稿が grep / diff 可能な形で残っている必要がある
- AP-09 (外部 AI 分析を検証せず records に取り込む) の再発防止

## 2. ディレクトリ構造

```
records/_external/
├── README.md
└── <source>/
    └── YYYY-MM-DD-<topic>.md
```

`<source>` の例:

- `chatgpt-5` / `chatgpt-4`
- `gemini-2-5-pro`
- `claude-opus-4`
- `<broker>-report` (例: `nomura-report`, `mufg-report`)
- `analyst-note`
- `news-summary`

## 3. ファイル front matter

```yaml
---
source: <文字列>            # 上記 §2 のいずれか
accessed_at: "YYYY-MM-DDTHH:MM:SS+09:00"
prompt_excerpt: |            # AI セッションの場合は最初の prompt 抜粋。レポート等は省略可
  <最初の数行>
related_tickers:             # 任意。research と紐付ける ticker 配列
  - "9682"
related_themes:              # 任意。トピック分類
  - "valuation-mean-reversion"
---

# <source> による <topic>

(本文を生原稿のまま貼り付ける。会話形式なら全ターン残す)
```

## 4. research との紐付け

`records/04-research/**.md` の front matter に `external_refs` 配列で参照する:

```yaml
external_refs:
  - records/_external/chatgpt-5/2026-05-04-9682-valuation.md
```

研究本文の `## 14. Source verification log` で、各 `external_refs[]` ごとに以下を構造化して残す:

| external_ref | 採用 | 修正 | 未採用 |
| --- | --- | --- | --- |
| `chatgpt-5/2026-05-04-9682` | 2026年3月期実績 / OpenAI 連携日 / 配当方針 | EPS 75.00 円で再計算 (元は 71 円) | Gartner / IDC 二次集計、同業 PER 表 |

## 5. 運用ルール

- 1 取得イベント = 1 ファイル。1 ファイルに複数銘柄が混在してもよいが `related_tickers` で全て列挙する
- 生原稿はそのまま残す。要約・整形しない (要約は research 本文に書く)
- review 後に結論を修正した場合も、生原稿ファイルを上書きしない。修正内容は
  research の source verification log に残す。再取得した別出力なら別ファイル
  `YYYY-MM-DD-<topic>-v2.md` を作る
- 個人情報 / 機密 (アクセストークン / 個人を特定する文字列) は除去する
- 既存ファイルを更新しない (再取得した場合は別ファイル `YYYY-MM-DD-<topic>-v2.md` を作る)

## 6. gitignore / 容量管理

現状は repo にコミットする運用とする。容量が 100 MB / files で 200 を超えたら、以下を検討する。

- `.gitattributes` で diff suppress
- 一部を `_external/_archive/` に移して古いものから外部 storage (S3 等) へ
- 取得時の自動要約付与 (生原稿 + AI 要約の両方を保存)

## 7. AP-09 との接続

外部 AI / 二次分析の取り込みは [`/docs/anti-patterns.md`](/docs/anti-patterns.md) §9 (AP-09) で
規定する。本ディレクトリへの保存は AP-09 チェックリストの一項目であり、保存なしで research に
反映するのは AP-09 違反として扱う。
