# stores/macro

provider factから再構築できるL1 macro fact storeを置くディレクトリです。

- 既定パス: `stores/macro/macro.sqlite`
- `baibai-engine macro` が必要な公式 API / CSV から取得した数値時系列を保存します
- SQLite 本体はrebuildableでありgit管理しません。judgmentであるMacro Contextはapplication DBが所有します
