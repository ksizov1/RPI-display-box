<#
.SYNOPSIS
    Live-deploy this working tree to a running Adiona-TV box over SSH.

.DESCRIPTION
    The ITERATION path, not the release path. A release is a flashed image
    (image/build-image.sh or the GitHub Actions build); this script replays the
    same repo -> box file mapping that
    image/pi-gen/stage-adiona/00-install/01-run.sh performs at build time, onto a
    box that is already provisioned and reachable on its Ethernet uplink. Keep
    the two in step: if 01-run.sh grows a file, add it here.

    What it deliberately does NOT do (reflash instead):
      * kernel cmdline / config.txt, Plymouth theme registration, service
        enablement, the tty1 getty mask - all boot-level, all already done on a
        provisioned box.
      * Wi-Fi AP settings. SSID_PREFIX / WIFI_BAND / WIFI_CHANNEL /
        WIFI_PASSPHRASE are consumed ONCE, when first-boot creates the
        NetworkManager AP profile. Use -FirstBoot to re-run that provisioning.

    Debian trixie's sshd has PerSourcePenalties: a burst of connections gets the
    source address temporarily blocked. So a deploy is ONE ssh connection - the
    payload tarball (which carries the install script inside it) is fed to ssh on
    stdin. Only -Status / -Probe / -Logs / -Follow open a second one.

    Requires ssh.exe and tar.exe (both ship with Windows 10/11) and passwordless
    sudo on the box. Key-based SSH auth is strongly recommended: one deploy is
    one password prompt, and -Logs/-Follow add another.

.PARAMETER Box
    Target hostname or IP. Defaults to $env:ADIONA_BOX, else adiona-tv-6ced.local.

.PARAMETER User
    SSH login. Defaults to $env:ADIONA_USER, else adionauser.

.PARAMETER NoConf
    Leave /etc/adiona/box.conf alone, preserving tuning done on the box.

.PARAMETER Packages
    Install any packages from the image's 00-packages list that the box is
    missing. REQUIRED after a change that adds a dependency - a file sync alone
    leaves the box unable to run the new code.

.PARAMETER FirstBoot
    Re-run first-boot provisioning, rebuilding the Wi-Fi AP profile from
    box.conf. SSID and hostname are MAC-derived, so they do not change.

.PARAMETER Restart
    Which services to restart afterwards: both (default), controller, kiosk, none.

.PARAMETER Logs
    After deploying, tail this many journal lines from both units. E.g. -Logs 60.

.PARAMETER Follow
    After deploying, follow the journal until Ctrl-C.

.PARAMETER SetupSudo
    One-time setup: grant this user passwordless sudo on the box by installing
    /etc/sudoers.d/010-adiona-nopasswd. Interactive - you type the box password
    once (image/pi-gen/config sets it). The new rule is validated with visudo
    BEFORE it is installed and the whole sudoers set is re-validated after, with
    the file removed if anything fails, so a bad edit cannot lock sudo out.

.PARAMETER SetupKey
    One-time setup: create a passphrase-less key at ~/.ssh/adiona_ed25519 and
    append it to the box's authorized_keys, then use it (with IdentitiesOnly, so
    ssh stops offering your personal key and asking for ITS passphrase) for every
    later run. Costs one last prompt. Your personal key is not touched.

.PARAMETER NoAgent
    Skip the ssh-agent handling and let ssh prompt for the key passphrase per
    connection.

.PARAMETER Status
    Report what the box is running and exit. No deploy.

.PARAMETER Probe
    Stop the kiosk, run adiona-player.sh --probe (is RTP arriving at all?), then
    restart the kiosk. No deploy.

.PARAMETER DryRun
    Show the plan - target, stamp, payload, remote arguments - and send nothing.

.EXAMPLE
    .\tools\deploy.ps1
    Push everything and restart both services.

.EXAMPLE
    .\tools\deploy.ps1 -Packages -Logs 60
    First deploy after a change that adds a dependency, then show the journal.

.EXAMPLE
    .\tools\deploy.ps1 -Restart controller -NoConf
    Web/controller iteration that keeps the box's own box.conf tuning.

.EXAMPLE
    .\tools\deploy.ps1 -Box 192.168.1.155 -Status
    Ask a specific box what it is running.

.EXAMPLE
    .\tools\deploy.ps1 -SetupSudo
    Run this once per box, before the first deploy.
#>

[CmdletBinding()]
param(
    [string] $Box = $(if ($env:ADIONA_BOX) { $env:ADIONA_BOX } else { 'adiona-tv-6ced.local' }),
    [string] $User = $(if ($env:ADIONA_USER) { $env:ADIONA_USER } else { 'adionauser' }),
    [switch] $NoConf,
    [switch] $Packages,
    [switch] $FirstBoot,
    [ValidateSet('both', 'controller', 'kiosk', 'none')]
    [string] $Restart = 'both',
    [int]    $Logs = 0,
    [switch] $Follow,
    [switch] $SetupSudo,
    [switch] $SetupKey,
    [switch] $NoAgent,
    [switch] $Status,
    [switch] $Probe,
    [switch] $DryRun
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Target = "$User@$Box"

# A dedicated, passphrase-less key for the boxes (see -SetupKey). Your personal
# key keeps its passphrase; this one exists so an appliance on a lab LAN can be
# deployed to without a prompt per connection. IdentitiesOnly matters: without it
# ssh ALSO offers ~/.ssh/id_ed25519 and prompts for that key's passphrase even
# though this one would have worked.
$DeployKey = Join-Path $env:USERPROFILE '.ssh\adiona_ed25519'

# BatchMode is left off on purpose: password auth is a legitimate way in here.
# AddKeysToAgent hands an unlocked key to the agent on first use, so a
# passphrase-protected key is typed once per boot rather than once per connection.
$BaseSshOpts = @('-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=accept-new',
                 '-o', 'AddKeysToAgent=yes')
$SshOpts = $BaseSshOpts
if (Test-Path $DeployKey) { $SshOpts += @('-i', $DeployKey, '-o', 'IdentitiesOnly=yes') }

function Say  { param([string] $Message) Write-Host "==> $Message" -ForegroundColor Cyan }
function Note { param([string] $Message) Write-Host "    $Message" -ForegroundColor DarkGray }
function Fail { param([string] $Message) Write-Host "deploy: $Message" -ForegroundColor Red; exit 1 }

function Get-Tool {
    param([string] $Name)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $cmd) { Fail "$Name not found on PATH (ships with Windows 10/11; install OpenSSH Client if missing)" }
    return $cmd.Source
}

function Initialize-SshAgent {
    # Get the key into the Windows ssh-agent so ssh stops asking for its
    # passphrase on every single connection. ssh-add exit codes: 0 = agent has
    # keys, 1 = agent reachable but empty, 2 = no agent.
    # With the dedicated deploy key in place there is no passphrase to cache.
    if ($NoAgent -or (Test-Path $DeployKey)) { return }
    $sshAdd = Get-Command 'ssh-add.exe' -ErrorAction SilentlyContinue
    if (-not $sshAdd) { return }

    # 2>$null: "Error connecting to agent" is the normal no-agent case, not news.
    & $sshAdd.Source -l 2>$null > $null
    if ($LASTEXITCODE -eq 0) { return }        # a key is already loaded

    if ($LASTEXITCODE -eq 2) {
        $svc = Get-Service ssh-agent -ErrorAction SilentlyContinue
        if (-not $svc) { return }
        if ($svc.StartType -eq 'Disabled') {
            Note 'ssh-agent is disabled, so the key passphrase is asked for every connection.'
            Note 'Enable it once (elevated):  Set-Service ssh-agent -StartupType Automatic'
            return
        }
        try { Start-Service ssh-agent -ErrorAction Stop } catch {
            Note 'could not start ssh-agent; continuing with per-connection prompts'
            return
        }
    }

    $key = Join-Path $env:USERPROFILE '.ssh\id_ed25519'
    if (-not (Test-Path $key)) { $key = Join-Path $env:USERPROFILE '.ssh\id_rsa' }
    if (-not (Test-Path $key)) { return }      # password auth; nothing to cache

    Say 'loading your SSH key into the agent (passphrase asked once, then cached)'
    & $sshAdd.Source $key
    if ($LASTEXITCODE -ne 0) { Note 'ssh-add failed; continuing with per-connection prompts' }
}

function Invoke-RemoteScript {
    # Send a shell script over stdin (`ssh <target> bash -s`) instead of passing it
    # as an argument. PowerShell strips embedded double quotes when it hands an
    # argument to a native exe, which silently mangles any non-trivial shell
    # one-liner; stdin is immune, so the script below can be written normally.
    param([string] $Script)
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("adiona-cmd-" + [Guid]::NewGuid().ToString('N').Substring(0, 8) + '.sh')
    try {
        [IO.File]::WriteAllText($tmp, ($Script -replace "`r`n", "`n"), (New-Object Text.UTF8Encoding($false)))
        $argLine = "$($SshOpts -join ' ') $Target bash -s"
        $p = Start-Process -FilePath $ssh -ArgumentList $argLine `
            -RedirectStandardInput $tmp -NoNewWindow -Wait -PassThru
        return $p.ExitCode
    }
    finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
}

# ---------------------------------------------------------------------------
# Debug-only modes. One connection each, no file transfer, so they are safe to
# run against a box mid-session.
# ---------------------------------------------------------------------------

# iw lives in /usr/sbin, which is NOT on a non-root user's PATH on Debian - hence
# the explicit path. The lease file is root-only, hence sudo (-SetupSudo first).
$StatusCmd = @'
echo "--- identity ---"
echo "hostname:         $(hostname)"
echo "ssid:             $(cat /etc/adiona/ssid 2>/dev/null || echo '?')"
echo "VERSION:          $(cat /opt/adiona/VERSION 2>/dev/null || echo '?')"
echo "last live deploy: $(cat /etc/adiona/.deployed 2>/dev/null || echo 'none - running the flashed image')"
echo "uptime:          $(uptime -p | sed 's/^up//')"
echo "--- services ---"
for u in adiona-controller adiona-kiosk; do
    printf "%-20s %s\n" "$u" "$(systemctl is-active "$u")"
done
echo "--- controller /state ---"
curl -s --max-time 3 http://127.0.0.1:8090/state || echo "(no answer on :8090)"
echo
echo "--- headsets ---"
echo "associated stations: $(/usr/sbin/iw dev wlan0 station dump 2>/dev/null | grep -c '^Station')"
sudo -n cat /var/lib/NetworkManager/dnsmasq-wlan0.leases 2>/dev/null \
    | awk '{ printf "  lease %-16s %s\n", $3, $2 }' || echo "  (lease file unreadable - run -SetupSudo)"
'@

# The kiosk session's own log lines are NOT reachable via `journalctl -u
# adiona-kiosk`. The unit sets PAMName=login, which puts its processes in a login
# session scope rather than the service cgroup, so they are journalled under the
# SYSLOG_IDENTIFIER of the ExecStart script (cage-session.sh) instead of the unit.
# `-u adiona-kiosk` returns zero player lines - verified on the box. The `+` is
# journalctl's disjunction: match the controller unit OR that identifier.
$JournalMatch = '_SYSTEMD_UNIT=adiona-controller.service + SYSLOG_IDENTIFIER=cage-session.sh'
$JournalFollowCmd = "journalctl -f --no-pager -n 20 $JournalMatch"
$JournalTailCmd   = "journalctl -n __N__ --no-pager $JournalMatch"

$ProbeCmd = @'
sudo systemctl stop adiona-kiosk
rc=0
/opt/adiona/kiosk/adiona-player.sh --probe || rc=$?
sudo systemctl start adiona-kiosk
exit $rc
'@

# One-time, interactive (-t, so sudo can prompt for the box password). The rule is
# validated before it is installed and the whole sudoers set after, reverting on
# failure: a malformed file in /etc/sudoers.d makes sudo refuse to run AT ALL,
# which on a box you can only reach over ssh is a reflash.
$SudoSetupCmd = @'
u=__USER__; t=$(mktemp); printf '%s ALL=(ALL) NOPASSWD: ALL\n' $u > $t; sudo visudo -cf $t || { rm -f $t; echo 'refused: generated rule is invalid'; exit 1; }; sudo install -m 0440 -o root -g root $t /etc/sudoers.d/010-adiona-nopasswd; rm -f $t; sudo visudo -c >/dev/null || { sudo rm -f /etc/sudoers.d/010-adiona-nopasswd; echo 'sudoers set failed validation - change reverted'; exit 1; }; if sudo -n true; then echo 'OK: passwordless sudo is active for' $u; else echo 'still prompting - inspect /etc/sudoers.d/010-adiona-nopasswd'; exit 1; fi
'@ -replace "`r`n", ' '

$ssh = Get-Tool 'ssh.exe'

if ($SetupKey) {
    # Authenticate the old way for this one run - the new key isn't installed yet.
    $SshOpts = $BaseSshOpts
    if (-not (Test-Path $DeployKey)) {
        Say "generating a passphrase-less deploy key at $DeployKey"
        $keygen = Get-Tool 'ssh-keygen.exe'
        & $keygen -t ed25519 -N '""' -C 'adiona-tv deploy' -f $DeployKey
        if ($LASTEXITCODE -ne 0) { Fail "ssh-keygen failed ($LASTEXITCODE)" }
    }
    else { Say "using the existing deploy key at $DeployKey" }

    $pub = (Get-Content "$DeployKey.pub" -Raw).Trim()
    Say "installing it on $Target (expect ONE last prompt)"
    # Double-quoted here-string so $pub interpolates - which means NO bash $(...)
    # in here, or PowerShell would try to evaluate it as a subexpression.
    $install = @"
mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
grep -qxF '$pub' ~/.ssh/authorized_keys || echo '$pub' >> ~/.ssh/authorized_keys
printf 'keys in authorized_keys: '
wc -l < ~/.ssh/authorized_keys
"@
    $rc = Invoke-RemoteScript $install
    if ($rc -ne 0) { Fail "could not install the key (exit $rc)" }
    Say 'done - subsequent runs should not prompt at all'
    exit 0
}

Initialize-SshAgent

if ($SetupSudo) {
    # The one mode that must stay interactive: sudo has to prompt for the box
    # password, so it needs a tty and cannot have stdin taken by a script.
    Say "granting passwordless sudo to $User on $Box (expect one password prompt)"
    & $ssh '-t' @SshOpts $Target ($SudoSetupCmd -replace '__USER__', $User)
    exit $LASTEXITCODE
}

if ($Status) {
    Say "status of $Target"
    exit (Invoke-RemoteScript $StatusCmd)
}

if ($Probe) {
    Say "probing RTP on $Target (kiosk stopped for the duration)"
    exit (Invoke-RemoteScript $ProbeCmd)
}

# ---------------------------------------------------------------------------
# The remote half. Mirrors 01-run.sh. Travels inside the tarball rather than on
# the command line, which keeps the ssh invocation short and quoting-free.
# ---------------------------------------------------------------------------

$InstallSh = @'
#!/usr/bin/env bash
# Generated by tools/deploy.ps1 - do not edit on the box; it is overwritten every
# deploy. Mirrors image/pi-gen/stage-adiona/00-install/01-run.sh.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
PUSH_CONF=1 DO_PACKAGES=0 DO_FIRSTBOOT=0 RESTART=both STAMP=""
while [ $# -gt 0 ]; do
	case "$1" in
		--no-conf)   PUSH_CONF=0 ;;
		--packages)  DO_PACKAGES=1 ;;
		--firstboot) DO_FIRSTBOOT=1 ;;
		--restart)   RESTART="$2"; shift ;;
		--stamp)     STAMP="$2"; shift ;;
	esac
	shift
done

sudo -n true 2>/dev/null || {
	echo "deploy: passwordless sudo is required on the box (stdin here is the payload," >&2
	echo "        so sudo cannot prompt). Add /etc/sudoers.d with NOPASSWD for this user." >&2
	exit 1
}

# Packages first: new code with an unmet dependency fails at runtime, not at copy
# time, and the failure (a black screen) looks nothing like its cause.
if [ "$DO_PACKAGES" = 1 ]; then
	PKGLIST="$SRC/image/pi-gen/stage-adiona/00-install/00-packages"
	missing=()
	while read -r pkg; do
		[ -n "$pkg" ] || continue
		case "$pkg" in \#*) continue ;; esac
		dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "ok installed" || missing+=("$pkg")
	done < "$PKGLIST"
	if [ ${#missing[@]} -eq 0 ]; then
		echo "  packages: all present"
	else
		echo "  packages: installing ${missing[*]}"
		sudo apt-get update -qq
		sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
	fi
fi

# Payload. The directories are removed rather than overlaid so files DELETED in
# the repo actually disappear from the box (web/jmuxer.js is the cautionary tale:
# left behind, it is a stale copy that nothing loads and everyone greps).
sudo rm -rf /opt/adiona/web /opt/adiona/controller /opt/adiona/first-boot /opt/adiona/kiosk
sudo install -d /opt/adiona /etc/adiona
sudo cp -r "$SRC/web"                "/opt/adiona/web"
sudo cp -r "$SRC/controller"         "/opt/adiona/controller"
sudo cp -r "$SRC/system/first-boot"  "/opt/adiona/first-boot"
sudo cp -r "$SRC/system/kiosk"       "/opt/adiona/kiosk"
sudo chmod +x /opt/adiona/kiosk/*.sh /opt/adiona/kiosk/*.py /opt/adiona/first-boot/*.sh
sudo install -m 0644 "$SRC/VERSION" /opt/adiona/VERSION
echo "  payload: /opt/adiona/{web,controller,first-boot,kiosk}"

if [ "$PUSH_CONF" = 1 ]; then
	if ! sudo diff -q /etc/adiona/box.conf "$SRC/config/box.conf" >/dev/null 2>&1; then
		echo "  box.conf changes (on-box -> repo; previous kept as box.conf.prev):"
		sudo diff -u /etc/adiona/box.conf "$SRC/config/box.conf" | sed 's/^/    /' || true
		sudo cp -f /etc/adiona/box.conf /etc/adiona/box.conf.prev 2>/dev/null || true
	fi
	sudo install -m 0644 "$SRC/config/box.conf" /etc/adiona/box.conf
else
	echo "  box.conf: left as-is (--no-conf)"
fi

# Units and system drop-ins.
sudo install -m 0644 "$SRC/system/controller/adiona-controller.service" \
                     "$SRC/system/kiosk/adiona-kiosk.service" \
                     "$SRC/system/first-boot/adiona-firstboot.service" /etc/systemd/system/
sudo install -m 0644 "$SRC/system/network/99-adiona-forward.conf" /etc/sysctl.d/
sudo install -m 0644 "$SRC/system/udev/99-adiona-no-pointer.rules" /etc/udev/rules.d/
sudo install -d /etc/NetworkManager/conf.d
sudo install -m 0644 "$SRC/system/networkmanager/10-adiona-wifi.conf" /etc/NetworkManager/conf.d/
sudo install -d /etc/chromium/policies/managed
sudo install -m 0644 "$SRC/system/chromium/adiona-policy.json" /etc/chromium/policies/managed/
sudo sysctl -q -p /etc/sysctl.d/99-adiona-forward.conf || true
# Wi-Fi powersave / MAC-randomisation settings only take effect once NM rereads
# its config. Reload rather than restart: restarting NM would drop the AP and
# with it any live stream.
sudo nmcli general reload conf 2>/dev/null || true
sudo udevadm control --reload-rules || true   # takes effect on the next replug

# Plymouth theme: files only. Registering a *different* theme needs an initramfs
# rebuild (plymouth-set-default-theme -R) and is a reflash-class change.
if [ -d /usr/share/plymouth/themes/adiona-tv ]; then
	sudo install -m 0644 "$SRC/system/plymouth/adiona-tv/adiona-tv.plymouth" \
	                     "$SRC/system/plymouth/adiona-tv/adiona-tv.script" \
	                     /usr/share/plymouth/themes/adiona-tv/
	sudo install -m 0644 "$SRC/web/splash.png" /usr/share/plymouth/themes/adiona-tv/splash.png
fi

printf '%s\n' "$STAMP" | sudo tee /etc/adiona/.deployed >/dev/null

if [ "$DO_FIRSTBOOT" = 1 ]; then
	echo "  first-boot: re-running provisioning (AP profile rebuilt from box.conf)"
	sudo rm -f /etc/adiona/.firstboot-done
	sudo systemctl start adiona-firstboot.service
fi

sudo systemctl daemon-reload
case "$RESTART" in
	both)       UNITS="adiona-controller adiona-kiosk" ;;
	controller) UNITS="adiona-controller" ;;
	kiosk)      UNITS="adiona-kiosk" ;;
	none)       UNITS="" ;;
esac
if [ -n "$UNITS" ]; then
	echo "  restarting: $UNITS"
	# shellcheck disable=SC2086
	sudo systemctl restart $UNITS
	sleep 2
	# shellcheck disable=SC2086
	for u in $UNITS; do printf '    %-20s %s\n' "$u" "$(systemctl is-active "$u")"; done
else
	echo "  restart: skipped (--restart none)"
fi
'@

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

$tar = Get-Tool 'tar.exe'

$PayloadItems = @(
    'web'
    'controller'
    'system'
    'config'
    'VERSION'
    'image/pi-gen/stage-adiona/00-install/00-packages'
)
foreach ($item in $PayloadItems) {
    $p = Join-Path $Root ($item -replace '/', '\')
    if (-not (Test-Path $p)) { Fail "missing from the working tree: $item" }
}

# Stamp the box with exactly what it is running - the first question every remote
# debugging session asks, and a working tree is not a commit.
$version = (Get-Content (Join-Path $Root 'VERSION') -TotalCount 1).Trim()
$gitRef = 'no-git'
$sha = & git -C $Root rev-parse --short HEAD 2>$null
if ($LASTEXITCODE -eq 0 -and $sha) {
    $gitRef = $sha.Trim()
    & git -C $Root diff --quiet
    if ($LASTEXITCODE -ne 0) { $gitRef = "$gitRef-dirty" }
}
$stamp = "v$version $gitRef deployed from $env:COMPUTERNAME at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
$stamp = $stamp -replace "['`"]", ''   # it rides in a single-quoted shell argument

$remoteArgs = "--restart $Restart --stamp '$stamp'"
if ($NoConf)    { $remoteArgs += ' --no-conf' }
if ($Packages)  { $remoteArgs += ' --packages' }
if ($FirstBoot) { $remoteArgs += ' --firstboot' }

if ($DryRun) {
    Say 'dry run - nothing sent'
    Write-Host "  target:   $Target"
    Write-Host "  stamp:    $stamp"
    Write-Host "  payload:  $($PayloadItems -join ' ')"
    Write-Host "  remote:   bash install.sh $remoteArgs"
    exit 0
}

# ---------------------------------------------------------------------------
# Stage -> tar -> one ssh connection
# ---------------------------------------------------------------------------

$stage = Join-Path ([IO.Path]::GetTempPath()) ("adiona-deploy-" + [Guid]::NewGuid().ToString('N').Substring(0, 8))
$tarball = "$stage.tgz"

try {
    Say "deploying v$version $gitRef -> $Target"

    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    foreach ($item in $PayloadItems) {
        $src = Join-Path $Root ($item -replace '/', '\')
        if (Test-Path $src -PathType Container) {
            Copy-Item $src -Destination (Join-Path $stage (Split-Path $item -Leaf)) -Recurse -Force
        }
        else {
            # keep the repo-relative path so install.sh can find 00-packages where it expects
            $dest = Join-Path $stage ($item -replace '/', '\')
            New-Item -ItemType Directory -Path (Split-Path $dest -Parent) -Force | Out-Null
            Copy-Item $src -Destination $dest -Force
        }
    }
    Get-ChildItem $stage -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force

    # install.sh must reach the Pi as LF/UTF-8-without-BOM or bash chokes on the
    # shebang; PowerShell's default writers give neither.
    $shText = ($InstallSh -replace "`r`n", "`n")
    [IO.File]::WriteAllText((Join-Path $stage 'install.sh'), $shText, (New-Object Text.UTF8Encoding($false)))

    & $tar '-czf' $tarball '-C' $stage '.'
    if ($LASTEXITCODE -ne 0) { Fail "tar failed ($LASTEXITCODE)" }

    $remoteCmd = "set -e; rm -rf /tmp/adiona-deploy; mkdir -p /tmp/adiona-deploy; " +
                 "tar xzf - -C /tmp/adiona-deploy; " +
                 "bash /tmp/adiona-deploy/install.sh $remoteArgs; " +
                 "rm -rf /tmp/adiona-deploy"

    # Start-Process, not a pipeline: Windows PowerShell reinterprets bytes flowing
    # between native commands as text, which corrupts the gzip stream. Redirecting
    # a file into stdin keeps it binary-clean. The password prompt (if any) still
    # works - ssh reads it from the console, not stdin.
    $argLine = "$($SshOpts -join ' ') $Target `"$remoteCmd`""
    $proc = Start-Process -FilePath $ssh -ArgumentList $argLine `
        -RedirectStandardInput $tarball -NoNewWindow -Wait -PassThru
    if ($proc.ExitCode -ne 0) { Fail "remote install failed (exit $($proc.ExitCode))" }

    Say 'done'
}
finally {
    if (Test-Path $stage)   { Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue }
    if (Test-Path $tarball) { Remove-Item $tarball -Force -ErrorAction SilentlyContinue }
}

# ---------------------------------------------------------------------------
# Optional journal tail (second connection)
# ---------------------------------------------------------------------------

if ($Follow) {
    Say 'following journal (Ctrl-C to stop)'
    & $ssh '-t' @SshOpts $Target $JournalFollowCmd
}
elseif ($Logs -gt 0) {
    & $ssh @SshOpts $Target ($JournalTailCmd -replace '__N__', $Logs)
}
