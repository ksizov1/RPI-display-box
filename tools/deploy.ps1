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
    stdin.

    -Status / -Probe / -Logs / -Follow are READ-ONLY: each opens one connection,
    does its job and exits WITHOUT deploying.

    Requires ssh.exe and tar.exe (both ship with Windows 10/11) and passwordless
    sudo on the box. Key-based SSH auth is strongly recommended: without it every
    connection costs a password prompt.

.PARAMETER Box
    Target hostname or IP. Defaults to $env:ADIONA_BOX, else adiona-tv-6ced.local.

    A name is resolved by trying, in order: the name as given (mDNS), the bare
    name without .local (which most routers answer from the DHCP lease, by a
    different mechanism that survives mDNS being broken), and the last IP that
    worked. Each candidate is proven with a TCP connect to sshd, so a name that
    resolves to a stale lease is rejected rather than hung on. The winning
    address is cached in %LOCALAPPDATA%\adiona-deploy\last-known-good.json.

    An IP given here is used as-is, no probing.

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
    Which services to restart afterwards: both (default: controller + kiosk +
    wheel), controller, kiosk, wheel, none.

.PARAMETER Logs
    Tail this many journal lines from all units and exit. E.g. -Logs 60.
    READ-ONLY: it does not deploy.

.PARAMETER Follow
    Follow the journal until Ctrl-C, and exit. READ-ONLY: it does not deploy.
    To deploy and then watch, run the deploy and then run -Follow - ideally in a
    second window opened BEFORE the thing you want to observe.

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

.PARAMETER Reflashed
    Everything a freshly flashed card needs, in one command: clear the stale SSH
    host keys, install the deploy key, then grant passwordless sudo.

    A new card regenerates its host keys on first boot and starts with no
    authorized_keys and no sudoers rule, so all three break at once. Clearing a
    CHANGED host key is deliberately not automatic anywhere else - it is the same
    warning a real interception produces, and it should only be waved away when
    you know you just reflashed the box, which is what passing this switch means.

    Expect two prompts: the SSH password once, then the sudo password once.

.PARAMETER NoAgent
    Skip the ssh-agent handling and let ssh prompt for the key passphrase per
    connection.

.PARAMETER Discover
    Find Adiona boxes on this machine's subnet and exit. No deploy.

    Names are unreliable here: .local depends on mDNS, the bare name depends on
    the router registering DHCP hostnames, and both have been observed failing
    while the box was perfectly reachable. There is also no PTR record to reverse.
    So this does not resolve anything - it sweeps the /24 for a listening sshd
    (about 2 s, all connections in flight at once) and then asks each responder
    who it is over SSH, using the deploy key with BatchMode so a machine that is
    not ours fails instantly instead of prompting.

    Identification is the box's own answer - /etc/adiona/ssid and
    /opt/adiona/VERSION - not a guess from its MAC. The MAC would not work: this
    box's uplink is the USB Wi-Fi dongle, so its address carries the DONGLE
    vendor's OUI, not Raspberry Pi's.

    Prints an ADIONA_BOX line you can paste, and caches the address so an
    immediately following deploy finds it without probing again.

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
    .\tools\deploy.ps1 -Packages
    First deploy after a change that adds a dependency.

.EXAMPLE
    .\tools\deploy.ps1 -Logs 60
    Show the last 60 journal lines. Deploys nothing.

.EXAMPLE
    .\tools\deploy.ps1 -Restart controller -NoConf
    Web/controller iteration that keeps the box's own box.conf tuning.

.EXAMPLE
    .\tools\deploy.ps1 -Box 192.168.1.155 -Status
    Ask a specific box what it is running.

.EXAMPLE
    .\tools\deploy.ps1 -Discover
    Find the boxes on this subnet when the name will not resolve.

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
    [ValidateSet('both', 'controller', 'kiosk', 'wheel', 'updater', 'none')]
    [string] $Restart = 'both',
    [int]    $Logs = 0,
    [switch] $Follow,
    [switch] $SetupSudo,
    [switch] $SetupKey,
    [switch] $Reflashed,
    [switch] $NoAgent,
    [switch] $Discover,
    [switch] $Status,
    [switch] $Probe,
    [switch] $DryRun
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
# $Target is set after Resolve-Box runs, further down.

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

# ---------------------------------------------------------------------------
# Finding the box.
#
# The default target is an mDNS name, and mDNS on Windows is unreliable in a way
# that has nothing to do with the box: the query is multicast to 224.0.0.251 and
# the answer has to return on the same interface, so a machine carrying dead
# APIPA adapters (a disconnected second NIC, Bluetooth PAN) can send it somewhere
# that will never answer, and which interface wins shifts as adapters come and
# go. A failed lookup is then negatively cached for minutes, and .local is also
# claimed by LLMNR and by Bonjour if some other app installed it.
#
# None of this touches the product: nothing in the streaming path resolves a name
# (the headset addresses the box by gateway IP, the box finds headsets from DHCP
# leases, the player just binds a port). It only breaks deploys, so it is fixed
# here rather than in the image.
#
# Three ways to find the same box, cheapest first. Each candidate is proven with
# a real TCP connect to sshd, which tests resolution and reachability together -
# a name that resolves to a stale lease is worse than one that does not resolve.
# ---------------------------------------------------------------------------

function Get-BoxCacheFile {
    $dir = Join-Path $env:LOCALAPPDATA 'adiona-deploy'
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    return (Join-Path $dir 'last-known-good.json')
}

function Get-CachedBoxIp {
    param([string] $Key)
    $f = Get-BoxCacheFile
    if (-not (Test-Path $f)) { return $null }
    try {
        $map = Get-Content $f -Raw | ConvertFrom-Json
        return $map.$Key
    } catch { return $null }
}

function Set-CachedBoxIp {
    param([string] $Key, [string] $Ip)
    if (-not $Ip) { return }
    $f = Get-BoxCacheFile
    $map = @{}
    if (Test-Path $f) {
        try { (Get-Content $f -Raw | ConvertFrom-Json).PSObject.Properties |
                ForEach-Object { $map[$_.Name] = $_.Value } } catch { }
    }
    $map[$Key] = $Ip
    try { $map | ConvertTo-Json | Set-Content -Path $f -Encoding utf8 } catch { }
}

function Test-BoxReachable {
    param([string] $HostName, [int] $TimeoutMs = 2500)
    $c = $null
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        # BeginConnect resolves the name itself, so an unresolvable host fails
        # here exactly like an unreachable one - which is what we want.
        $iar = $c.BeginConnect($HostName, 22, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne($TimeoutMs)) { return $false }
        $c.EndConnect($iar)
        return $true
    } catch { return $false }
    finally { if ($c) { $c.Close() } }
}

function Get-Ipv4For {
    param([string] $HostName)
    $ip = $null
    if ([System.Net.IPAddress]::TryParse($HostName, [ref] $ip)) { return $HostName }
    try {
        return ([System.Net.Dns]::GetHostAddresses($HostName) |
                Where-Object { $_.AddressFamily -eq 'InterNetwork' } |
                Select-Object -First 1).IPAddressToString
    } catch { return $null }
}

# Sweep the local /24 for boxes and ask each candidate who it is.
#
# Deliberately not name-based. On this network .local and the bare name have both
# been seen to fail while the box was up and answering on its IP, and there is no
# PTR record, so nothing can be reversed either. Positive identification comes from
# the box telling us its SSID and version.
function Find-Boxes {
    param([int] $SweepWaitMs = 2500)

    $local = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
             Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
             Sort-Object -Property SkipAsSource, InterfaceMetric |
             Select-Object -First 1
    if (-not $local) { Fail 'no usable IPv4 interface found' }
    $prefix = ($local.IPAddress -split '\.')[0..2] -join '.'
    Say "sweeping $prefix.0/24 for a listening sshd (from $($local.IPAddress))"

    # All 254 connections started before any is awaited, so the sweep costs one
    # wait rather than 254 timeouts.
    $pending = @{}
    foreach ($i in 1..254) {
        $ip = "$prefix.$i"
        if ($ip -eq $local.IPAddress) { continue }
        $c = New-Object System.Net.Sockets.TcpClient
        try { $pending[$ip] = @{ Client = $c; Task = $c.ConnectAsync($ip, 22) } }
        catch { $c.Close() }
    }
    Start-Sleep -Milliseconds $SweepWaitMs

    $listening = @()
    foreach ($ip in $pending.Keys) {
        if ($pending[$ip].Task.Status -eq 'RanToCompletion') { $listening += $ip }
        $pending[$ip].Client.Close()
    }
    if (-not $listening) {
        Note 'nothing on this subnet is listening on :22'
        return @()
    }
    Note ("ssh open on: " + (($listening | Sort-Object) -join ', '))

    # BatchMode so a non-box fails immediately rather than waiting on a password
    # prompt. UserKnownHostsFile=NUL keeps a survey of the whole subnet from
    # writing an entry per machine into your known_hosts.
    $found = @()
    foreach ($ip in ($listening | Sort-Object)) {
        # LogLevel=ERROR suppresses ssh's "Permanently added ..." notice, and
        # $ErrorActionPreference is dropped to Continue for the call: in Windows
        # PowerShell anything a native command writes to stderr becomes a
        # NativeCommandError, which under 'Stop' is a TERMINATING error - so a
        # single chatty ssh invocation would abort the whole survey. Failure is
        # detected from the exit code instead, which is what we check.
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $probe = & $ssh '-i' $DeployKey '-o' 'IdentitiesOnly=yes' '-o' 'BatchMode=yes' `
                            '-o' 'ConnectTimeout=4' '-o' 'StrictHostKeyChecking=no' `
                            '-o' 'UserKnownHostsFile=NUL' '-o' 'LogLevel=ERROR' "$User@$ip" `
                            'cat /etc/adiona/ssid 2>/dev/null; cat /opt/adiona/VERSION 2>/dev/null' 2>$null
            $rc = $LASTEXITCODE
        }
        finally { $ErrorActionPreference = $prevEap }
        if ($rc -ne 0 -or -not $probe) { continue }
        $lines = @($probe | Where-Object { $_ -and $_.Trim() } | ForEach-Object { $_.Trim() })
        $ssid = @($lines | Where-Object { $_ -like 'Adiona-*' })[0]
        if (-not $ssid) { continue }
        $ver = @($lines | Where-Object { $_ -match '^\d+\.\d+\.\d+' })[0]
        $found += [pscustomobject]@{
            Ip       = $ip
            Ssid     = $ssid
            Version  = $(if ($ver) { $ver } else { '?' })
            HostName = $ssid.ToLower()
        }
    }
    return $found
}

function Resolve-Box {
    param([string] $Configured)

    $parsed = $null
    if ([System.Net.IPAddress]::TryParse($Configured, [ref] $parsed)) { return $Configured }

    $tried = @()
    $candidates = @([pscustomobject]@{ Host = $Configured; How = 'mDNS name' })
    if ($Configured -like '*.local') {
        # Not mDNS at all: most routers register DHCP client hostnames in their
        # own DNS, so the bare name resolves by a completely separate mechanism
        # and survives mDNS being broken.
        $candidates += [pscustomobject]@{ Host = ($Configured -replace '\.local$', ''); How = 'router DNS (short name)' }
    }
    $cached = Get-CachedBoxIp $Configured
    if ($cached) { $candidates += [pscustomobject]@{ Host = $cached; How = 'last known good IP' } }

    foreach ($c in $candidates) {
        if (Test-BoxReachable $c.Host) {
            if ($c.How -ne 'mDNS name') { Note "found via $($c.How): $($c.Host)" }
            Set-CachedBoxIp $Configured (Get-Ipv4For $c.Host)
            return $c.Host
        }
        $tried += "$($c.Host) [$($c.How)]"
    }

    Fail (@"
could not reach $Configured on port 22. Tried:
      $($tried -join "`n      ")
    The box may be off, still booting, or on another network. If you know its
    address, use it directly - it is cached for next time:
      .\tools\deploy.ps1 -Box 192.168.1.168
      `$env:ADIONA_BOX = '192.168.1.168'   # for the rest of the session
"@)
}

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
echo "image version:    $(cat /etc/adiona/image-version 2>/dev/null || echo 'pre-OTA image')"
echo "layout:           $(if [ -L /opt/adiona/current ]; then echo "release ($(readlink /opt/adiona/current))"; else echo 'flat (never OTA-updated)'; fi)"
echo "releases on card: $(ls /opt/adiona/releases 2>/dev/null | tr '\n' ' ' || echo '-')"
echo "last live deploy: $(cat /etc/adiona/.deployed 2>/dev/null || echo 'none - running the flashed image')"
echo "last OTA update:  $(cat /etc/adiona/.updated 2>/dev/null || echo 'none')"
if [ -f /etc/adiona/update-pending ]; then
    echo "!! UPDATE IN FLIGHT OR INTERRUPTED:"
    sed 's/^/     /' /etc/adiona/update-pending
fi
echo "uptime:          $(uptime -p | sed 's/^up//')"
echo "--- services ---"
for u in adiona-controller adiona-kiosk adiona-wheel adiona-updater; do
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
# adiona-apply is the transient unit systemd-run creates for an update; its output
# is the only record of what an update did, so it belongs in the same view.
$JournalMatch = '_SYSTEMD_UNIT=adiona-controller.service + _SYSTEMD_UNIT=adiona-wheel.service + _SYSTEMD_UNIT=adiona-updater.service + SYSLOG_IDENTIFIER=cage-session.sh + SYSLOG_IDENTIFIER=adiona-apply'
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

# Locate the box before anything needs $Target. Every mode below - deploy, the
# setup switches, the debug modes - goes through this, so none of them can fail
# on a name lookup that the next candidate would have solved.
$ConfiguredBox = $Box
$Box = Resolve-Box $Box
$Target = "$User@$Box"

function Reset-KnownHosts {
    param([string[]] $Hosts)
    # A reflashed card regenerates its SSH host keys on first boot, so ssh
    # refuses to connect with REMOTE HOST IDENTIFICATION HAS CHANGED. That is the
    # correct behaviour and the same warning a real interception produces, which
    # is exactly why clearing it lives behind an explicit switch rather than
    # being done for you. Entries can be filed under any of the names the box has
    # been reached by, so clear them all.
    $keygen = Get-Tool 'ssh-keygen.exe'
    # ssh-keygen -R writes "Host ... not found in known_hosts" to STDERR when
    # there is nothing to remove, which is a normal outcome here. Windows
    # PowerShell turns native stderr into a NativeCommandError, and this script
    # runs with ErrorActionPreference=Stop, so that benign message aborted the
    # whole run. Ask first with -F (silent, exit code only) and relax the
    # preference around the native calls.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        foreach ($h in ($Hosts | Where-Object { $_ } | Select-Object -Unique)) {
            $present = $false
            try {
                & $keygen -F $h 2>$null | Out-Null
                $present = ($LASTEXITCODE -eq 0)
            } catch { $present = $false }

            if ($present) {
                try { & $keygen -R $h 2>$null | Out-Null } catch { }
                Note "cleared stale known_hosts entry for $h"
            }
        }
    }
    finally { $ErrorActionPreference = $prev }
}

function Invoke-SetupKey {
    # Authenticate the old way for this one run - the new key isn't installed yet.
    $script:SshOpts = $BaseSshOpts
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
    # The key is on the box now, so anything that runs after this in the same
    # invocation (see -Reflashed) should authenticate with it.
    $script:SshOpts = $BaseSshOpts
    if (Test-Path $DeployKey) { $script:SshOpts += @('-i', $DeployKey, '-o', 'IdentitiesOnly=yes') }
    return 0
}

function Invoke-SetupSudo {
    # The one mode that must stay interactive: sudo has to prompt for the box
    # password, so it needs a tty and cannot have stdin taken by a script.
    Say "granting passwordless sudo to $User on $Box (expect one password prompt)"
    # Out-Host is load-bearing. A native command's stdout goes to PowerShell's
    # SUCCESS stream, so in a function whose value is assigned - which both callers
    # do - a bare `& ssh ...` makes the remote's output part of the return value.
    # This function then returns @('...parsed OK', 'OK: ...', 0) instead of 0, and
    # `if ($rc -ne 0)` on an array is true whenever ANY element is non-zero, so a
    # completely successful setup reported:
    #     deploy: sudo setup failed (exit /tmp/x: parsed OK OK: ... 0)
    # Routing the output to the host keeps it visible and leaves only the exit code
    # as the return value. Invoke-SetupKey is unaffected because it goes through
    # Invoke-RemoteScript, whose Start-Process -NoNewWindow writes straight to the
    # console and returns a bare ExitCode.
    & $ssh '-t' @SshOpts $Target ($SudoSetupCmd -replace '__USER__', $User) | Out-Host
    return $LASTEXITCODE
}

# Everything a freshly flashed card needs, in one command. Key first, then sudo:
# with the key already installed, the sudo step authenticates with it, so the two
# prompts are the SSH password once and the sudo password once - rather than the
# SSH password twice plus sudo.
if ($Reflashed) {
    Say "post-reflash recovery for $Target"
    Reset-KnownHosts @($ConfiguredBox,
                       ($ConfiguredBox -replace '\.local$', ''),
                       (Get-CachedBoxIp $ConfiguredBox),
                       $Box)
    $rc = Invoke-SetupKey
    if ($rc -ne 0) { Fail "key setup failed (exit $rc)" }
    Initialize-SshAgent
    $rc = Invoke-SetupSudo
    if ($rc -ne 0) { Fail "sudo setup failed (exit $rc)" }
    Say 'box is reachable again - run .\tools\deploy.ps1 to push the working tree'
    exit 0
}

if ($SetupKey) { exit (Invoke-SetupKey) }

Initialize-SshAgent

if ($SetupSudo) { exit (Invoke-SetupSudo) }

if ($Discover) {
    $boxes = Find-Boxes
    if (-not $boxes) {
        Write-Host ''
        Fail @"
no Adiona boxes found on this subnet.
      The box may be off, on another network, or reachable only through its own
      AP. If you know its address:
        .\tools\deploy.ps1 -Box 192.168.1.153
"@
    }
    Write-Host ''
    foreach ($b in $boxes) {
        Write-Host ("  {0,-16} {1,-18} v{2}" -f $b.Ip, $b.Ssid, $b.Version) -ForegroundColor Green
    }
    Write-Host ''
    # Cache it against the configured name so a deploy straight after this one
    # finds the box even while the name is still not resolving.
    Set-CachedBoxIp $Box $boxes[0].Ip
    Write-Host 'To use the first one for the rest of this session:'
    Write-Host ("  `$env:ADIONA_BOX = '{0}'" -f $boxes[0].Ip) -ForegroundColor Cyan
    exit 0
}

if ($Status) {
    Say "status of $Target"
    exit (Invoke-RemoteScript $StatusCmd)
}

# Read-only, like -Status and -Probe: they EXIT here rather than falling through to
# the deploy. -Follow used to sit at the bottom of the file and therefore meant
# "deploy, THEN follow" - a flag whose name says it only watches, silently pushing
# a new build to the box. That is a genuinely dangerous shape for a command you
# reach for while something else is mid-flight: it cost a whole OTA test, because
# the box was quietly moved to the version that was supposed to be the update.
# To deploy and then watch, run the deploy and then run -Follow - preferably in a
# second window, started BEFORE the thing you want to observe.
if ($Follow) {
    Say "following journal on $Target (Ctrl-C to stop; this does NOT deploy)"
    & $ssh '-t' @SshOpts $Target $JournalFollowCmd
    exit 0
}

if ($Logs -gt 0) {
    Say "last $Logs journal lines from $Target (this does NOT deploy)"
    & $ssh @SshOpts $Target ($JournalTailCmd -replace '__N__', $Logs)
    exit 0
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

# Same lock apply-update.sh takes. Two writers to /opt/adiona at once produce an
# undefined tree, and "I deployed while the box happened to be installing an OTA
# update" is not a state anyone should have to reason about afterwards.
#
# The file is created BY ROOT and opened for READING. A redirection is performed
# by the shell itself, which here is the unprivileged deploy user, so `exec 9>` on
# a root-owned /opt/adiona fails with EACCES before sudo ever enters the picture.
# flock does not need write access — the lock is advisory and lives on the inode —
# so a read-only fd takes the same exclusive lock and still mutually excludes the
# root-owned applier, which opens the identical file for writing.
sudo install -d /opt/adiona
sudo touch /opt/adiona/.update.lock
exec 9</opt/adiona/.update.lock
if ! flock -n 9; then
	echo "deploy: an OTA update is applying on the box; retry in a minute." >&2
	exit 1
fi

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

# Payload. Two layouts exist and this has to handle both:
#
#   flat    — /opt/adiona/{web,controller,...} are real directories, as the image
#             build creates them. A box that has never taken an OTA update.
#   release — those names are symlinks into /opt/adiona/current, which points at
#             one of /opt/adiona/releases/<version>. Created by the first update.
#
# On a release-layout box we must NOT write through the symlinks: `rm -rf
# /opt/adiona/web` would delete the contents of the CURRENT RELEASE directory and
# `cp -r` would then fill it with a working tree, so a release named 1.6.0 would
# quietly contain somebody's uncommitted edits — and a later rollback would
# restore them. Build a new release directory instead and repoint `current`,
# which also makes a live deploy rollback-able like any other release.
#
# Either way the directories are replaced rather than overlaid, so files DELETED
# in the repo actually disappear from the box (web/jmuxer.js is the cautionary
# tale: left behind, it is a stale copy that nothing loads and everyone greps).
VER="$(head -1 "$SRC/VERSION" | tr -d '[:space:]')"
sudo install -d /opt/adiona /etc/adiona

if [ -L /opt/adiona/current ]; then
	# The +deploy suffix marks this as a working-tree build rather than a signed
	# release, so it is obvious in -Status and in the update UI what is running.
	REL="/opt/adiona/releases/${VER}+deploy"
	PREV="$(readlink /opt/adiona/current)"
	sudo rm -rf "$REL"
	sudo install -d "$REL/config" "$REL/units" "$REL/units-installed"
	sudo cp -r "$SRC/web"                "$REL/web"
	sudo cp -r "$SRC/controller"         "$REL/controller"
	sudo cp -r "$SRC/system/first-boot"  "$REL/first-boot"
	sudo cp -r "$SRC/system/kiosk"       "$REL/kiosk"
	sudo cp -r "$SRC/system/wheel"       "$REL/wheel"
	sudo cp -r "$SRC/system/updater"     "$REL/updater"
	sudo install -m 0644 "$SRC/VERSION" "$REL/VERSION"
	sudo install -m 0644 "$SRC/config/box.conf" "$REL/config/box.conf"
	sudo find "$SRC/system" -name '*.service' -exec sudo install -m 0644 {} "$REL/units/" \;
	# What is live right now, so a rollback to this deploy restores its units too.
	for u in /etc/systemd/system/adiona-*.service; do
		[ -f "$u" ] && sudo cp -a "$u" "$REL/units-installed/"
	done
	sudo chmod +x "$REL"/kiosk/*.sh "$REL"/kiosk/*.py "$REL"/first-boot/*.sh \
	              "$REL"/wheel/*.py "$REL"/updater/*.py "$REL"/updater/*.sh
	sync
	sudo ln -sfn "releases/${VER}+deploy" /opt/adiona/.current.tmp
	sudo mv -T /opt/adiona/.current.tmp /opt/adiona/current
	echo "  payload: $REL (release layout; current was $PREV)"
else
	sudo rm -rf /opt/adiona/web /opt/adiona/controller /opt/adiona/first-boot \
	            /opt/adiona/kiosk /opt/adiona/wheel /opt/adiona/updater
	sudo cp -r "$SRC/web"                "/opt/adiona/web"
	sudo cp -r "$SRC/controller"         "/opt/adiona/controller"
	sudo cp -r "$SRC/system/first-boot"  "/opt/adiona/first-boot"
	sudo cp -r "$SRC/system/kiosk"       "/opt/adiona/kiosk"
	sudo cp -r "$SRC/system/wheel"       "/opt/adiona/wheel"
	sudo cp -r "$SRC/system/updater"     "/opt/adiona/updater"
	sudo chmod +x /opt/adiona/kiosk/*.sh /opt/adiona/kiosk/*.py \
	              /opt/adiona/first-boot/*.sh /opt/adiona/wheel/*.py \
	              /opt/adiona/updater/*.py /opt/adiona/updater/*.sh
	sudo install -m 0644 "$SRC/VERSION" /opt/adiona/VERSION
	echo "  payload: /opt/adiona/{web,controller,first-boot,kiosk,wheel,updater}"
fi

# Boxes flashed before OTA existed have neither file, and an updater with no key
# rejects every manifest — so this is what turns updates on for the existing fleet.
#
# image-version records what the CARD WAS FLASHED WITH, and a live deploy cannot
# know that: it changes /opt/adiona, never the boot configuration. Writing the
# version being deployed would be a lie that lets a `min_image` gate pass on a box
# whose kernel cmdline, config.txt and Plymouth registration are years older —
# exactly the half-applied state min_image exists to prevent. So record 0.0.0,
# meaning "predates image-version, unknown". Releases that set min_image will
# refuse to install and say the card needs re-imaging, which is true; releases
# that do not set it are unaffected. A reflash writes the real value.
[ -f /etc/adiona/image-version ] || \
	echo "0.0.0" | sudo tee /etc/adiona/image-version >/dev/null
sudo install -m 0644 "$SRC/system/updater/update-key.pub" /etc/adiona/update-key.pub

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
                     "$SRC/system/wheel/adiona-wheel.service" \
                     "$SRC/system/updater/adiona-updater.service" \
                     "$SRC/system/updater/adiona-rollback.service" \
                     "$SRC/system/first-boot/adiona-firstboot.service" /etc/systemd/system/
# Enable here as well as in the image stage: boxes flashed before the LAN wheel
# existed have no enablement for it, and a live deploy is the only way they will
# ever get one short of a reflash.
sudo systemctl enable adiona-wheel.service >/dev/null 2>&1 || true
sudo systemctl enable adiona-updater.service >/dev/null 2>&1 || true
sudo systemctl enable adiona-rollback.service >/dev/null 2>&1 || true
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
	both)       UNITS="adiona-controller adiona-kiosk adiona-wheel adiona-updater" ;;
	controller) UNITS="adiona-controller" ;;
	kiosk)      UNITS="adiona-kiosk" ;;
	wheel)      UNITS="adiona-wheel" ;;
	updater)    UNITS="adiona-updater" ;;
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

# -Follow and -Logs are handled far above, before the deploy, and exit there.
# Nothing to do here.
