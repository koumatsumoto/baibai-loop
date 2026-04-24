# journal/

世界情勢・相場環境の調査記録を時系列で蓄積するディレクトリ。

## 命名規則

```
journal/YYYY/MM/YYYY-MM-DD-{kind}-{slug}.md
```

- `{kind}` 例: `world-weekly` / `macro-monthly` / `fomc` / `boj` / `cpi` / `gdp` / `geopolitics`
- `{slug}` は内容を端的に示す短い英小文字ハイフン区切り

例:

```
journal/2026/04/2026-04-19-world-weekly-us-cpi-beat.md
journal/2026/04/2026-04-macro-monthly-us-cpi-hot.md
journal/2026/04/2026-04-30-fomc-hold.md
```

## 運用ルール

- 設計根拠: [../docs/design-principles.md](../docs/design-principles.md)
- 週次テンプレート: [../docs/templates/world-analysis.md](../docs/templates/world-analysis.md)
- 月次テンプレート: [../docs/templates/macro-monthly.md](../docs/templates/macro-monthly.md)
- 更新頻度・引用形式: [../docs/workflow.md](../docs/workflow.md)
- データソース: [../docs/data-sources.md](../docs/data-sources.md)

INDEX ファイルは設けない。一覧は `git ls-files journal/` または GitHub ツリーで確認する。
