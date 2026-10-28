param(
    [Parameter(Mandatory=$true)][string]$Session,
    [string]$Root = "data\hand_flex3_checks\dual_pilot"
)
$ErrorActionPreference = "Stop"
if ($Session -notmatch '^[A-Za-z0-9_-]+$') { throw "Use a session name, not a path." }
$rawRoot = (Resolve-Path -LiteralPath $Root).Path
$stage = Join-Path ([System.IO.Path]::GetTempPath()) ("glove_export_" + [guid]::NewGuid().ToString("N"))
$downloads = Join-Path $env:USERPROFILE "Downloads"
New-Item -ItemType Directory -Path $downloads -Force | Out-Null
New-Item -ItemType Directory -Path $stage | Out-Null
try {
    $parts = @()
    foreach ($hand in @("left", "right")) {
        $source = Join-Path (Join-Path $rawRoot $hand) $Session
        if (Test-Path -LiteralPath $source -PathType Container) {
            $handStage = Join-Path $stage $hand
            New-Item -ItemType Directory -Path $handStage | Out-Null
            Copy-Item -LiteralPath $source -Destination $handStage -Recurse
            $parts += $handStage
        }
    }
    if ($parts.Count -eq 0) { throw "Session not found under LEFT or RIGHT: $Session" }
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
    $archive = Join-Path $downloads ("${Session}_${stamp}.zip")
    Compress-Archive -LiteralPath $parts -DestinationPath $archive
    Write-Host "Saved: $archive"
    Write-Host "Included $($parts.Count) hand folder(s). Original recordings retained."
}
finally {
    Remove-Item -LiteralPath $stage -Recurse -Force
}
