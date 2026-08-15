<#
.SYNOPSIS
    Cut a release: bump VERSION, commit, tag and push - in the one order that works.

.DESCRIPTION
    Releasing by hand is five ordered steps, and skipping or reordering any of them
    fails late and confusingly:

      1. write VERSION            - miss it and CI rejects the tag ("does not match")
      2. commit it                - miss it and the tag points at the OLD version
      3. push the commit          - miss it and the tag references an unknown commit
      4. create the tag           - on the commit that carries the bump, not before
      5. push the tag             - which is what actually triggers the build

    The failure mode that keeps happening is a commit MESSAGE that says v1.6.2 while
    the VERSION file inside it still says 1.6.1. The message is not the version; the
    file is. This script writes the file, verifies it reads back, and runs the exact
    check CI runs BEFORE pushing anything - so a mismatch costs seconds locally
    instead of a failed build and a tag that has to be deleted from the remote.

    Why VERSION is not simply derived from the tag: deploy.ps1 stamps boxes from the
    working tree on untagged dev builds, the pi-gen stage copies it to
    /etc/adiona/image-version, and adiona-updater compares against it. It has to
    exist as a file in the tree, so the file and the tag must be kept in step.

.PARAMETER Version
    The version to release, x.y.z. Omit to bump the patch component of the current
    VERSION (1.6.1 -> 1.6.2).

.PARAMETER Retag
    Move a tag that already exists. Deletes it locally and on the remote first.
    Refused without this switch, because moving a published tag rewrites what a
    release points at.

.PARAMETER DryRun
    Print the plan and change nothing.

.PARAMETER Yes
    Skip the confirmation prompt.

.EXAMPLE
    .\tools\release.ps1                 # 1.6.1 -> 1.6.2
    .\tools\release.ps1 1.7.0
    .\tools\release.ps1 1.6.2 -Retag    # fix a tag pointing at the wrong commit
    .\tools\release.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string] $Version,
    [switch] $Retag,
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
if ($nv -eq $cv -and -not $Retag) {
    Fail "VERSION is already $current. Pass a new version, or -Retag to re-cut this one."
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

if (($localTag -or $remoteTag) -and -not $Retag) {
    $where = @(); if ($localTag) { $where += 'locally' }; if ($remoteTag) { $where += 'on the remote' }
    Fail "$Tag already exists $($where -join ' and '). Pass -Retag to move it."
}

# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "  VERSION      $current -> $Version"
Write-Host "  commit       `"$Version`" on $branch"
if ($localTag -or $remoteTag) {
    Write-Host "  tag          $Tag (DELETED and re-created)" -ForegroundColor Yellow
} else {
    Write-Host "  tag          $Tag"
}
Write-Host "  push         origin $branch, then origin $Tag"
Write-Host "  triggers     release-tarball (minutes) + image build (45-75 min)"
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

# The exact check CI runs, run here first.
if ("v$readBack" -ne $Tag) { Fail "VERSION '$readBack' does not match tag '$Tag'" }
Say "VERSION is $readBack and matches $Tag"

$r = Invoke-Git add -- VERSION
if ($r.Code -ne 0) { Fail "git add failed: $($r.Out)" }
$r = Invoke-Git commit -m $Version
if ($r.Code -ne 0) { Fail "git commit failed: $($r.Out)" }
$head = (Invoke-Git rev-parse --short HEAD).Out
Say "committed $head"

# Push the commit BEFORE the tag: a tag pushed first would reference an object the
# remote has never seen.
$r = Invoke-Git push origin $branch
if ($r.Code -ne 0) { Fail "git push failed: $($r.Out)" }
Say "pushed $branch"

if ($localTag)  { Invoke-Git tag -d $Tag | Out-Null; Say "deleted local $Tag" }
if ($remoteTag) {
    $r = Invoke-Git push origin ":refs/tags/$Tag"
    if ($r.Code -ne 0) { Fail "could not delete the remote tag: $($r.Out)" }
    Say "deleted remote $Tag"
}

$r = Invoke-Git tag $Tag
if ($r.Code -ne 0) { Fail "git tag failed: $($r.Out)" }

# Prove the tag really carries the bump before it goes anywhere.
$tagged = (Invoke-Git show "${Tag}:VERSION").Out.Trim()
if ($tagged -ne $Version) { Fail "tag $Tag carries VERSION '$tagged', not $Version" }
Say "$Tag points at $head, whose VERSION is $tagged"

$r = Invoke-Git push origin $Tag
if ($r.Code -ne 0) { Fail "could not push the tag: $($r.Out)" }
Say "pushed $Tag - the build is running"

Write-Host ""
Write-Host "Next:" -ForegroundColor Green
Write-Host "  1. wait for release-tarball, then take adiona-tv-$Version.tar.gz and"
Write-Host "     manifest-fragment.json from the RELEASES page (not the run's Artifacts)"
Write-Host "  2. scp the tarball to /var/www/license-api-binaries/5/ on the licence server"
Write-Host "  3. paste the fragment into data/box_versions.json - write a real 'notes'"
Write-Host "     line, and leave min_image empty unless the release needs a reflash"
Write-Host "  4. push the licence server, then: bash tools/verify-signing.sh"
Write-Host "  5. restart adiona-updater on a box and answer the prompt on the TV"
