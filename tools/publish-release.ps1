<#
.SYNOPSIS
    Publish an Adiona-TV OTA release: upload the tarball to the AWS builds CDN
    (primary) and the licence server VM (mirror), and write the licence server's
    data/box_versions.json.

.DESCRIPTION
    The box-side counterpart of Adiona-G's deploy_win.ps1 / deploy_web.ps1, and it
    uses the same bucket and CloudFront distribution:

        s3://adiona-builds-242344399348-us-east-1/rpi/adiona-tv-<version>.tar.gz
        https://djmxvs3blcla1.cloudfront.net/rpi/adiona-tv-<version>.tar.gz   <- "url"
        https://license.drivingsimulator.com/5/adiona-tv-<version>.tar.gz     <- "mirrors"

    1. Takes adiona-tv-<version>.tar.gz and box_versions.json from the GitHub
       release CI built (Releases/v<version>), or from -Dist. The CI-built tarball
       is the canonical one: its sha256 is what the manifest promises.
    2. Checks the tarball against the manifest's size and sha256 before uploading
       anything.
    3. Uploads it to S3 under rpi/. Versioned names are immutable and cached for a
       year, so an existing object is never overwritten; if it is already there
       and byte-identical (a re-run), the upload is skipped.
    4. Copies it to the VM mirror over scp (skip with -SkipMirror). The VM stays a
       fallback until it is retired; boxes only reach it if the CDN fails.
    5. Writes the manifest - with your -Notes and -Rollout - over
       <LicenseServerRepo>/data/box_versions.json. Review, commit and push the
       licence server to make it live, then run tools/verify-signing.sh.

    Needs: the AWS CLI logged in to the d2-marketing account (`aws login`), gh
    (unless -Dist), scp (unless -SkipMirror), and Node.js (rewrites the JSON).

.PARAMETER Notes
    One line shown on the TV in the update prompt.

.PARAMETER MirrorHost
    ssh target of the licence server VM, e.g. user@host. Defaults to
    $env:ADIONA_MIRROR_SSH.

.EXAMPLE
    .\tools\publish-release.ps1 -Notes "Faster casting start. Wheel fixes."
    .\tools\publish-release.ps1 -Notes "Canary." -Rollout 10
    .\tools\publish-release.ps1 -Notes "..." -SkipMirror
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $Notes,
    [string] $Version = "",
    [ValidateRange(0, 100)]
    [int]    $Rollout = 100,
    # Outputs of the d2-marketing "adiona-web-cdn" CloudFormation stack (us-east-1);
    # the same values as Adiona-G's deploy_win.ps1.
    [string] $Bucket = "adiona-builds-242344399348-us-east-1",
    [string] $CdnBase = "https://djmxvs3blcla1.cloudfront.net",
    [string] $MirrorHost = $env:ADIONA_MIRROR_SSH,
    [string] $MirrorDir = "/var/www/license-api-binaries/5",
    [string] $LicenseServerRepo = "",
    [string] $Dist = "",
    [switch] $SkipMirror
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
if (-not $LicenseServerRepo) { $LicenseServerRepo = Join-Path (Split-Path -Parent $Root) 'Adiona-license-server' }

function Say  { param([string] $m) Write-Host "==> $m" -ForegroundColor Cyan }
function Fail { param([string] $m) Write-Host "publish: $m" -ForegroundColor Red; exit 1 }

# Native commands report progress on stderr; with 'Stop' in force Windows
# PowerShell would turn that into a terminating error. Judge by exit code instead.
function Invoke-Native {
    param([string] $Exe, [string[]] $Arguments)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Exe @Arguments; return $LASTEXITCODE }
    finally { $ErrorActionPreference = $prev }
}

if (-not $Version) { $Version = (Get-Content (Join-Path $Root 'VERSION') -TotalCount 1).Trim() }
if ($Version -notmatch '^\d+\.\d+\.\d+$') { Fail "'$Version' is not x.y.z" }
if ($Notes -match 'TODO') { Fail "-Notes still says TODO - it is what the operator reads on the TV" }
if (-not $SkipMirror -and -not $MirrorHost) {
    Fail "no VM mirror target. Pass -MirrorHost user@host (or set ADIONA_MIRROR_SSH), or -SkipMirror."
}

$name = "adiona-tv-$Version.tar.gz"
$key = "rpi/$name"
$cdnUrl = "$($CdnBase.TrimEnd('/'))/$key"
$versionsFile = Join-Path $LicenseServerRepo 'data/box_versions.json'
if (-not (Test-Path (Split-Path -Parent $versionsFile))) { Fail "$LicenseServerRepo has no data/ - pass -LicenseServerRepo" }

# ── 1. artefacts ─────────────────────────────────────────────────────────────
if ($Dist) {
    $work = (Resolve-Path $Dist).Path
} else {
    $work = Join-Path $env:TEMP "adiona-tv-publish-$Version"
    if (Test-Path $work) { Remove-Item -Recurse -Force $work }
    New-Item -ItemType Directory -Path $work | Out-Null
    Say "downloading Releases/v$Version"
    $rc = Invoke-Native gh @('release', 'download', "v$Version", '--dir', $work,
                             '--pattern', $name, '--pattern', 'box_versions.json',
                             '--repo', (& git -C $Root remote get-url origin))
    if ($rc -ne 0) { Fail "gh release download failed - has CI published v$Version yet?" }
}
$tarball = Join-Path $work $name
$manifestPath = Join-Path $work 'box_versions.json'
foreach ($f in $tarball, $manifestPath) { if (-not (Test-Path $f)) { Fail "missing $f" } }

# ── 2. the tarball must be the one the manifest describes ────────────────────
$manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
$rel = @($manifest.releases | Where-Object { $_.version -eq $Version })[0]
if (-not $rel) { Fail "box_versions.json lists no v$Version release" }
$sha = (Get-FileHash -Algorithm SHA256 $tarball).Hash.ToLower()
$size = (Get-Item $tarball).Length
if ($sha -ne $rel.sha256.ToLower()) { Fail "tarball sha256 $sha does not match the manifest's $($rel.sha256)" }
if ($size -ne [int64]$rel.size) { Fail "tarball is $size bytes, the manifest says $($rel.size)" }
if ($rel.url -ne $cdnUrl) { Fail "the manifest's url is $($rel.url), expected $cdnUrl - was it built by an older make-release.sh?" }
Say ("v{0}: {1:N0} bytes, sha256 {2}" -f $Version, $size, $sha)

# ── 3. S3 (primary) ──────────────────────────────────────────────────────────
$prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
aws s3api head-object --bucket $Bucket --key $key 2>$null | Out-Null
$exists = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prev
if ($exists) {
    # Immutable: never overwrite. A re-run after a later step failed is fine as
    # long as what is there is byte-identical.
    $probe = Join-Path $work "cdn-$name"
    Invoke-WebRequest -Uri $cdnUrl -OutFile $probe -UseBasicParsing
    $have = (Get-FileHash -Algorithm SHA256 $probe).Hash.ToLower()
    Remove-Item $probe
    if ($have -ne $sha) { Fail "s3://$Bucket/$key already exists with DIFFERENT content - bump VERSION (published versions are immutable)" }
    Say "s3://$Bucket/$key already there and identical - skipping upload"
} else {
    Say "uploading to s3://$Bucket/$key"
    $rc = Invoke-Native aws @('s3', 'cp', $tarball, "s3://$Bucket/$key",
                              '--content-type', 'application/gzip',
                              '--cache-control', 'public, max-age=31536000, immutable',
                              '--only-show-errors')
    if ($rc -ne 0) { Fail "S3 upload failed (is the AWS CLI logged in? try: aws login)" }
}

# ── 4. VM (mirror) ───────────────────────────────────────────────────────────
if ($SkipMirror) {
    Write-Host "    skipping the VM mirror (-SkipMirror)" -ForegroundColor Yellow
} else {
    Say "copying to the mirror $MirrorHost`:$MirrorDir/"
    $rc = Invoke-Native scp @($tarball, "$MirrorHost`:$MirrorDir/$name")
    if ($rc -ne 0) { Fail "scp to the VM mirror failed (use -SkipMirror to publish to S3 only)" }
}

# ── 5. the licence server's manifest ─────────────────────────────────────────
# Node rather than ConvertTo-Json: Windows PowerShell's serialiser reflows the
# file and escapes characters, which makes the reviewed diff unreadable.
$env:ADIONA_BOX_NOTES = $Notes
$env:ADIONA_BOX_ROLLOUT = "$Rollout"
$env:ADIONA_BOX_DROP_MIRRORS = $(if ($SkipMirror) { '1' } else { '' })
$js = @'
const fs = require('fs');
const [src, dst] = process.argv.slice(1);
const m = JSON.parse(fs.readFileSync(src, 'utf8'));
for (const r of m.releases) {
  r.notes = process.env.ADIONA_BOX_NOTES;
  r.rollout = Number(process.env.ADIONA_BOX_ROLLOUT);
  if (process.env.ADIONA_BOX_DROP_MIRRORS) r.mirrors = [];
}
fs.writeFileSync(dst, JSON.stringify(m, null, 2) + '\n');
'@
$rc = Invoke-Native node @('-e', $js, $manifestPath, $versionsFile)
Remove-Item Env:ADIONA_BOX_NOTES, Env:ADIONA_BOX_ROLLOUT, Env:ADIONA_BOX_DROP_MIRRORS
if ($rc -ne 0) { Fail "could not write $versionsFile" }

# ── 6. can a box actually fetch it? ──────────────────────────────────────────
$urls = @($cdnUrl)
if (-not $SkipMirror) { $urls += @($rel.mirrors) }
foreach ($u in $urls) {
    try {
        $r = Invoke-WebRequest -Uri $u -Method Head -UseBasicParsing
        $len = [int64]$r.Headers['Content-Length']
        if ($len -ne $size) { Fail "$u serves $len bytes, expected $size" }
        Write-Host "    ok  $u" -ForegroundColor Green
    } catch {
        Fail "$u is not downloadable: $($_.Exception.Message)"
    }
}

Write-Host ""
Write-Host "Published v$Version" -ForegroundColor Green
Write-Host "  url      $cdnUrl"
if (-not $SkipMirror) { foreach ($m in @($rel.mirrors)) { Write-Host "  mirror   $m" } }
Write-Host "  wrote    $versionsFile"
Write-Host ""
Write-Host "Next: review that file, commit and push the licence server, then"
Write-Host "      bash tools/verify-signing.sh"
