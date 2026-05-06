# records/_playbooks/

Baibai-Loop で運用中の playbook snapshot 集合。各 historical record は dated immutable snapshot を参照する。

## Snapshot layout

`records/_playbooks/<playbook_id>/YYYY-MM-DDTHHMMSS+0900.md`

Research / decision register / trade は mutable alias ではなく snapshot path と content hash を持つ。
