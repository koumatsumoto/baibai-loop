# records/_playbooks/

Baibai-Loop で運用中の playbook 集合。各 historical record は判断時に使った repo 内 file path を参照する。

## Playbook Layout

`records/_playbooks/<playbook_id>/YYYY-MM-DDTHHMMSS+0900.md`

Research / decision register / trade は mutable alias ではなく、参照した playbook の repo 内 file path を持つ。content hash は持たない。
