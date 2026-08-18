<#
.SYNOPSIS
    Cut a release: bump VERSION, commit and push. CI does the rest.

.DESCRIPTION
    The version lives in ONE place - the VERSION file - and everything else is
    derived from it:

        .\tools\release.ps1 1.6.4      ->  writes VERSION, commits, pushes
        CI (on push to main)           ->  tags v1.6.4, builds the OTA tarball,
                                           publishes the GitHub release

    You never type a tag. That is deliberate: the tag and VERSION used to be the
    same fact typed twice, they drifted twice, and each time it failed in CI rather
    than locally. Now CI reads VERSION and creates the matching tag, so they cannot
    disagree.

    A push that does NOT change VERSION is a no-op for CI - ordinary work lands on
    main without producing releases.

    This script still exists for the part CI cannot do: writing the file correctly
    (LF, no BOM), reading it back to prove the write landed, and refusing to
    release from a dirty tree.

    The SD-card image is NOT built by a push. Start the workflow by hand from the
    Actions tab when a new card is needed; it attaches the .img.xz to the release
    its VERSION already names.

.PARAMETER Version
    The version to release, x.y.z. Omit to bump the patch component of the current
    VERSION (1.6.3 -> 1.6.4).

.PARAMETER DryRun
    Print the plan and change nothing.

.PARAMETER Yes
    Skip the confirmation prompt.

.EXAMPLE
    .\tools\release.ps1                 # 1.6.3 -> 1.6.4
    .\tools\release.ps1 1.7.0
    .\tools\release.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string] $Version,
    [switch] $DryRun,
    [switch] $Yes
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$VersionFile = Join-Path $Root 'VERSION'

function Say  { param([string] $m) Write-Host "==> $m" -ForegroundColor Cyan }
function Warn { param([string] $m) Write-Host "    $m" -ForegroundColor Yellow }
function Fail { param([string] $m) Write-Host "release: $m" -ForegroundColor Red; exit 1 }

# Resolved once, and called through the variable. A function named `Git` that
# calls `& git` calls ITSELF: PowerShell resolves a bare command name Alias ->
# Function -> Cmdlet -> Application, so the function wins and recurses until the
# session hangs. deploy.ps1 sidesteps the same trap by resolving ssh.exe up front.
$GitExe = (Get-Command 'git.exe' -CommandType Application -ErrorAction SilentlyContinue |
           Select-Object -First 1).Source
if (-not $GitExe) { Write-Host 'release: git.exe not found on PATH' -ForegroundColor Red; exit 1 }

function Invoke-Git {
    # Two Windows PowerShell traps, both of which turn a working git command into a
    # script that dies:
    #
    #   * `2>&1` wraps each stderr line in a NativeCommandError, so it is not used
    #     here - stderr goes straight to the console, where it is useful anyway;
    #   * even without it, $ErrorActionPreference='Stop' promotes anything a native
    #     command writes to stderr into a TERMINATING error. `git push` reports its
    #     progress ("To github.com...", "* [new tag]") on stderr on every SUCCESSFUL
    #     push, so with Stop in force the script would abort at the push and never
    #     reach the exit-code check below.
    #
    # Failure is detected from the exit code, which is what the caller inspects.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $GitExe -C $Root @args
        return [pscustomobject]@{ Code = $LASTEXITCODE; Out = ($out -join "`n").Trim() }
    }
    finally { $ErrorActionPreference = $prev }
}

# ---------------------------------------------------------------------------
# Work out what we are releasing
# ---------------------------------------------------------------------------

if (-not (Test-Path $VersionFile)) { Fail "no VERSION file at $VersionFile" }
$current = (Get-Content $VersionFile -TotalCount 1).Trim()
if ($current -notmatch '^\d+\.\d+\.\d+$') { Fail "VERSION is '$current', which is not x.y.z" }

if (-not $Version) {
    $p = $current.Split('.')
    $Version = "{0}.{1}.{2}" -f $p[0], $p[1], ([int]$p[2] + 1)
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') { Fail "'$Version' is not x.y.z" }
$Tag = "v$Version"

# Refuse to go backwards by accident. Comparing component-wise, because string
# comparison puts 1.10.0 before 1.9.0.
$cv = [version]$current
$nv = [version]$Version
if ($nv -lt $cv) { Fail "$Version is older than the current $current" }
if ($nv -eq $cv) {
    Fail "VERSION is already $current - CI would see no change and publish nothing. Pass a new version."
}

Say "releasing $current -> $Version (tag $Tag)"

# ---------------------------------------------------------------------------
# Preflight. Everything that can be checked before touching anything.
# ---------------------------------------------------------------------------

$branch = (Invoke-Git rev-parse --abbrev-ref HEAD).Out
if ($branch -ne 'main') { Warn "on branch '$branch', not 'main'" }

# A dirty tree means the tag would capture work you did not mean to release, or
# omit work you did.
$dirty = (Invoke-Git status --porcelain).Out
if ($dirty) {
    Write-Host "    uncommitted changes:" -ForegroundColor Yellow
    $dirty -split "`n" | ForEach-Object { Write-Host "      $_" -ForegroundColor Yellow }
    Fail "commit or stash these first - a release must be reproducible from its tag"
}

$localTag  = (Invoke-Git rev-parse -q --verify "refs/tags/$Tag").Code -eq 0
$remoteRaw = Invoke-Git ls-remote --tags origin $Tag
if ($remoteRaw.Code -ne 0) { Fail "cannot reach the remote: $($remoteRaw.Out)" }
$remoteTag = [bool]$remoteRaw.Out

if ($localTag -or $remoteTag) {
    $where = @(); if ($localTag) { $where += 'locally' }; if ($remoteTag) { $where += 'on the remote' }
    Fail @"
$Tag already exists $($where -join ' and ') - that version has been released.
      CI creates tags from VERSION, so it would see nothing new and publish
      nothing. Pick a higher version.
"@
}

# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "  VERSION      $current -> $Version"
Write-Host "  commit       `"$Version`" on $branch"
Write-Host "  push         origin $branch"
Write-Host "  then CI      tags $Tag, builds the OTA tarball, publishes the release"
Write-Host "  NOT built    the SD-card image (start it by hand from the Actions tab)"
Write-Host ""

if ($DryRun) { Say 'dry run - nothing changed'; exit 0 }

if (-not $Yes) {
    $answer = Read-Host "Proceed? [y/N]"
    if ($answer -notmatch '^(y|yes)$') { Say 'aborted'; exit 0 }
}

# ---------------------------------------------------------------------------
# Do it
# ---------------------------------------------------------------------------

# LF and no BOM. .gitattributes normalises on commit, but CI reads the file with
# `head -1 VERSION`, the pi-gen stage installs it verbatim as
# /etc/adiona/image-version, and a BOM there would make the first character
# invisible-but-present in every version comparison on every box.
[IO.File]::WriteAllText($VersionFile, "$Version`n", (New-Object Text.UTF8Encoding($false)))

# Read it back rather than trusting the write. This is the step whose silent
# failure produced a commit titled "v1.6.2" containing VERSION 1.6.1.
$readBack = (Get-Content $VersionFile -TotalCount 1).Trim()
if ($readBack -ne $Version) { Fail "wrote $Version but VERSION reads '$readBack'" }

# The same derivation CI performs, done here first so a bad write is caught on
# this machine rather than in a build log.
if ("v$readBack" -ne $Tag) { Fail "VERSION '$readBack' does not match tag '$Tag'" }
Say "VERSION is $readBack; CI will tag $Tag"

$r = Invoke-Git add -- VERSION
if ($r.Code -ne 0) { Fail "git add failed: $($r.Out)" }
$r = Invoke-Git commit -m $Version
if ($r.Code -ne 0) { Fail "git commit failed: $($r.Out)" }
$head = (Invoke-Git rev-parse --short HEAD).Out
Say "committed $head"

# No tagging here. CI reads VERSION from this commit and creates the tag itself,
# which is what makes the two impossible to disagree.
$r = Invoke-Git push origin $branch
if ($r.Code -ne 0) { Fail "git push failed: $($r.Out)" }
Say "pushed $branch - CI will tag $Tag and publish the release"

Write-Host ""
Write-Host "Next:" -ForegroundColor Green
Write-Host "  1. wait a couple of minutes, then open Releases/$Tag and take"
Write-Host "     adiona-tv-$Version.tar.gz and box_versions.json"
Write-Host "  2. scp the tarball to /var/www/license-api-binaries/5/ on the licence server"
Write-Host "  3. write a real 'notes' line in box_versions.json, then use it to"
Write-Host "     REPLACE data/box_versions.json there - it is the whole file"
Write-Host "  4. push the licence server, then: bash tools/verify-signing.sh"
Write-Host "  5. power-cycle a box and answer the prompt on the TV"
Write-Host ""
Write-Host "  (SD-card image: not built by this push. Start the workflow by hand" -ForegroundColor DarkGray
Write-Host "   from the Actions tab; it attaches the .img.xz to Releases/$Tag.)" -ForegroundColor DarkGray
