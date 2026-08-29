# stores/application

`baibai.sqlite` は judgment、ledger、thesis、assessment、operation、task を保持する
canonical application DB です。再生成可能な cache ではありません。backup は
`uv run baibai-engine db backup` で `backups/` に作成します。
