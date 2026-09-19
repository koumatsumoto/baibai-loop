# Tunnelの常駐・WSL自動起動

Owner MCPを常時利用するには、Tunnelをsystemd user serviceで動かし、Windowsログイン時に
WSLを起動・維持します。Tunnelが異常終了すると10秒後、WSL維持taskが終了すると1分後に再起動します。
PCの電源断・スリープ・Windowsログアウト中の接続は保証しません。Windows再起動後はログインが必要です。

## 起動設定の場所と前提

この手順は、[READMEの初期設定](./README.md#準備と起動)を済ませた既存のローカルwrapperを使います。
一時worktreeではなく、継続して使うmain checkoutを作業directoryにします。

| 場所 | 内容 |
| --- | --- |
| `<repo>/.cache/owner-mcp/tunnel-client` | checksum確認済みのTunnel client。本手順の確認対象は0.0.14 |
| `<repo>/.cache/owner-mcp/tunnel-launch.py` | Tunnel runtime設定だけを渡してclientへexecする親wrapper |
| `<repo>/.cache/owner-mcp/launch.py` | MCPへREAD_ACCESS_TOKENだけを渡す子wrapper |
| `<repo>/.cache/owner-mcp/entry.py` | repository rootでOwner MCPを起動するentry |
| `<repo>/.env` | wrapperが読むローカル認証設定。MCP module自体は自動読込しない |
| `~/.config/systemd/user/baibai-mcp-tunnel.service` | Linux側の常駐設定 |
| Windows task `Baibai MCP WSL Keepalive` | ログイン時のWSL起動維持 |

wrapper・binary・認証設定はGit管理外です。`.cache/owner-mcp`はこの構成の起動に必要なので、
通常のcache掃除で消さないでください。見つからない場合は既存worktreeを含めて配置を確認し、
初期設定を復元してから常駐化します。checkoutを移す場合はwrapper内の絶対pathとserviceを更新します。

親wrapperが必要とする設定名は`CONTROL_PLANE_API_KEY`、`CONTROL_PLANE_TUNNEL_ID`、
`CONTROL_PLANE_ORGANIZATION_ID`です。子wrapperは`READ_ACCESS_TOKEN`を使います。
値をunit file、task引数、Git、issue、PRへ書きません。wrapperは通常shell全体の環境を引き継がず、
それぞれに必要な認証設定だけを渡します。API keyにはTunnels Read / Useを付与します。

## Ubuntuで常駐化する

通常のUbuntu terminalで実行します。`/etc/wsl.conf`の`[boot]`に`systemd=true`が必要です。
変更が必要な場合は、他のWSL作業を終了してからWindows側で`wsl --shutdown`し、Ubuntuを開き直します。
[Microsoftのsystemd手順](https://learn.microsoft.com/en-us/windows/wsl/systemd)も参照してください。

まずrepository rootで起動fileと依存を確認します。認証値は表示しません。

```bash
test -x .cache/owner-mcp/tunnel-client
test -f .cache/owner-mcp/tunnel-launch.py
test -f .cache/owner-mcp/launch.py
test -f .cache/owner-mcp/entry.py
uv sync --frozen --group mcp
.cache/owner-mcp/tunnel-client run --help
```

各確認が成功し、手動起動中の同じTunnelがない状態で登録します。
unitが既存なら内容を確認し、別の設定を上書きしません。

```bash
repo_root="$(pwd -P)"
install -d -m 700 ~/.config/systemd/user
cat > ~/.config/systemd/user/baibai-mcp-tunnel.service <<EOF
[Unit]
Description=Baibai Owner MCP Secure Tunnel
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory="$repo_root"
ExecStart="$repo_root/.venv/bin/python" "$repo_root/.cache/owner-mcp/tunnel-launch.py"
Restart=always
RestartSec=10
TimeoutStopSec=30
UMask=0077

[Install]
WantedBy=default.target
EOF
chmod 600 ~/.config/systemd/user/baibai-mcp-tunnel.service
systemd-analyze --user verify ~/.config/systemd/user/baibai-mcp-tunnel.service
systemctl --user daemon-reload
systemctl --user enable --now baibai-mcp-tunnel.service
loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger
```

`enable-linger`が権限不足なら、そのcommandだけ`sudo`で実行します。期待値は`Linger=yes`です。
serviceの`enable`はUbuntu起動時の自動起動、lingerはLinuxのユーザーsessionに依存しない稼働を設定します。

## Windowsログイン時にWSLを起動・維持する

systemdの登録だけではWindowsログイン時のWSL起動・維持を設定できません。
Windows PowerShellで`wsl --list --quiet`を確認し、以下の`Ubuntu`を実際のdistribution名に合わせます。
Linux userはそのdistributionの既定userを使います。serviceを登録したuserが既定userであることを確認します。

```powershell
$ErrorActionPreference = 'Stop'
$name = 'Baibai MCP WSL Keepalive'
if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
    throw 'Task already exists; inspect before modifying.'
}
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction `
    -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -Argument '-NoProfile -NonInteractive -WindowStyle Hidden -Command "& wsl.exe --distribution Ubuntu --exec /bin/sleep infinity; exit 1"'
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description 'Keep Ubuntu WSL running for the Baibai MCP tunnel.'
Start-ScheduledTask -TaskName $name
Get-ScheduledTask -TaskName $name
```

taskは通常user権限で動き、Windows passwordを保存しません。`sleep infinity`はWSLを維持し、
Tunnel本体はsystemdが管理します。WSLが終了した場合はactionを失敗扱いにしてtaskの再試行を使います。
taskが`Running`でもTunnelの接続成功は別途確認します。PCのスリープ設定は変更しません。

## 確認・再起動・切断時の復旧

Ubuntuでserviceの状態を確認します。

```bash
systemctl --user status baibai-mcp-tunnel.service
systemctl --user show baibai-mcp-tunnel.service -p UnitFileState -p NRestarts
```

`enabled`かつ`active (running)`を確認します。既存wrapperはloopbackのhealth URLを
`.cache/owner-mcp/health.url`へ、logを`launch.log`と`tunnel.log`へ出します。
repository rootでhealthを確認します。

```bash
health_url="$(cat .cache/owner-mcp/health.url)"
curl --fail --silent --show-error "$health_url/healthz"
curl --fail --silent --show-error "$health_url/readyz"
```

期待値はHTTP 200と`live` / `ready`です。その後ChatGPTで`l1_resolve_current`を呼び、
実際のMCP接続も確認します。tool一覧が古い場合は[metadataのRefresh](./README.md#tool定義の更新)を行います。

接続できない場合はWindows task → Linux service → health / ready → ChatGPTの順で確認します。
serviceが起動しない場合は`journalctl --user -u baibai-mcp-tunnel.service`とwrapperのlogを確認します。
logを共有する前に秘密値・個人識別情報を除きます。認証や起動pathの修正後は次で再起動します。

```bash
systemctl --user restart baibai-mcp-tunnel.service
```

異常終了後の復旧を確認する場合は、MCPを利用していない時間に
`systemctl --user kill --kill-whom=main --signal=SIGKILL baibai-mcp-tunnel.service`を一度実行します。
10秒以上待ち、`NRestarts`増加、新しいPID、health / readyの復旧を確認します。
PC再起動の確認は他の作業を終了してから行い、Windowsログイン後に同じ確認を繰り返します。

## 停止・自動起動の解除

一時停止はUbuntuで`systemctl --user stop baibai-mcp-tunnel.service`を実行します。
明示的なstopでは`Restart=always`でも再起動しません。

自動起動を解除するときはUbuntuで以下を実行します。

```bash
systemctl --user disable --now baibai-mcp-tunnel.service
```

続けてWindows PowerShellでWSL維持taskを解除します。

```powershell
$name = 'Baibai MCP WSL Keepalive'
Stop-ScheduledTask -TaskName $name
Unregister-ScheduledTask -TaskName $name -Confirm:$false
```

taskを止めてもWSL内の既存processがすべて終了するとは限りません。WSLも止める場合は、他の作業がないことを確認して
`wsl --terminate Ubuntu`を実行します。lingerは他のuser serviceも使うため、不要であることを確認した場合だけ
`loginctl disable-linger "$USER"`で解除します。認証設定やstoreは削除しません。


## data tools更新後の受入

稼働checkoutへ変更を取り込んだ後、既存Tunnelのmetadataを[READMEのRefresh手順](./README.md#tool定義の更新)で更新し、新しいChatで更新したtool名とschemaを確認します。
PRのfixture/local store検証だけでは、この受入を完了したと扱いません。Refresh待ちは未完了として記録します。

catalog → macro.reading → 複数系列の履歴 → provider_run → exact Macro ContextをTriageなしで辿り、
reading refの再取得を確認します。L1 resolve/describe/queryは従来のfixed releaseで確認します。
保存runの全Security Analysisはcursorで最後まで取得し、ordinal・件数を照合します。
calibrationは同じsnapshot_tokenでcohort/panel/forwardを確認し、不在なら生成せず不在と記録します。
L3は実在するrecordだけを読み、未登録のpublicationを受入のために作りません。
