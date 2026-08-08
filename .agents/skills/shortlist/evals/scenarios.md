# Shortlist scenario bank

## Cloud publication reuse and zero-selection completion

- **対象層** — shortlist preflight、canonical previous、follow-up trigger、selected 0 の operation completion
- **題材** — 2026-08-09、ASOF 2026-08-07。cloud batch は succeeded / published、run と checked-out commit は一致し、同一 as-of の local run も存在する。greatest prior as-of に複数 revision があり、application DB の canonical shortlist だけが一方を束縛する。全 entry を rejected とし、1 件は過去 event を財務へ反映済みで将来 estimated date があり、もう 1 件は次回日程が無い。read-only sandbox で実行コマンド、判断、follow-up、final payload、報告を生成する。
- **期待品質** — cloud の run / selection を再利用して同日 run を作らない。canonical shortlist の束縛先を previous にする。消化済みの過去日 task を作らず、将来 estimated date または undated condition へ進む。selected 0 は `completion_reason: no-shortlist-selection` と `selected_count: 0` artifact で表し、`human_confirmation` を偽装しない。
- **判定** — 採用（2026-08-09、#869）。blind A/B で candidate が目的達成・scope・成果物品質・事故リスクの全軸で優位。
- **トレードオフ / 注記** — preflight の 1 手が増えるが、独自 SQL 探索と重複 run / select を置き換えるため標準 cycle の手順総数は増やさない。

## Selected candidates remain active

- **対象層** — selected がある通常 cycle の session 終端
- **題材** — preflight で既存 cloud publication を再利用し、OP3 narrative を持つ selected 2 件を publish する標準 cycle。
- **期待品質** — primary-research set の人間選択を待つため opportunity session を active に保つ。zero-selection completion を適用せず、追加の completion 儀式を持ち込まない。
- **判定** — 回帰題材として採用（2026-08-09、#869）。
