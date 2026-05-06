# Security Policy

このリポジトリは個人運用の trading workflow であり、外部公開の脆弱性報告窓口を持ちません。security 関連の指摘がある場合はリポジトリの issue で連絡してください。

依存ライブラリの脆弱性監査は CI で `pip-audit` を、source コードの静的解析は `bandit` を実行しています。詳細は [`docs/reference/python-foundation.md`](./docs/reference/python-foundation.md) §8 を参照。
