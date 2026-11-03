param(
    [Parameter(Mandatory=$true)]
    [ValidateSet('PICKUP','DROP','CHOP','WASH','THROW','POURING','IDLE')]
    [string]$Gesture,

    [ValidateSet('BOTH','LEFT','RIGHT')]
    [string]$Hands = 'BOTH',

    [ValidateRange(10,180)]
    [int]$Seconds = 30,

    [string]$Work = '.comms-local/hand_repaired_drop_g2000_v1_candidate2',

    [string]$Person = 'P01'
)

$ErrorActionPreference = 'Stop'
$python = '.\.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $python)) {
    throw 'Run this script from the repository root; .venv\Scripts\python.exe was not found.'
}

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss_fff'
$out = Join-Path $Work ("live_expected_{0}_{1}" -f $Gesture, $stamp)
$metadataTemp = Join-Path $Work ("expected_{0}_{1}.metadata.json" -f $Gesture, $stamp)
$consoleTemp = Join-Path $Work ("expected_{0}_{1}.console.txt" -f $Gesture, $stamp)

$metadata = [ordered]@{
    expected_gesture = $Gesture
    hands = $Hands
    seconds = $Seconds
    person = $Person
    candidate = (Resolve-Path -LiteralPath $Work).Path
    created_local = (Get-Date).ToString('o')
    note = 'Expected gesture is a test annotation only; it never changes model predictions.'
}
$metadata | ConvertTo-Json | Set-Content -LiteralPath $metadataTemp -Encoding UTF8

Write-Host ''
Write-Host "Expected gesture: $Gesture | Hands: $Hands | Duration: $Seconds seconds" -ForegroundColor Cyan
Write-Host 'This run is tagged for review only. The model still predicts every gesture.'
Write-Host "Output: $out"
Write-Host ''
switch ($Gesture) {
    'PICKUP'  { Write-Host 'Action: grasp/lift the imaginary object, then return to rest.' }
    'DROP'    { Write-Host 'Action: hold the imaginary object still, then open/release without a throw.' }
    'CHOP'    { Write-Host 'Action: one clear downward chop, then return to rest.' }
    'WASH'    { Write-Host 'Action: one clear circular/orb motion, then return to rest.' }
    'THROW'   { Write-Host 'Action: forward/chest thrust and release, then return to rest.' }
    'POURING' { Write-Host 'Action: start holding the imaginary pot, tilt and hold, then return to rest.' }
    'IDLE'    { Write-Host 'Action: remain still with only small natural finger/grip adjustments.' }
}
Read-Host 'Close other BLE programs, prepare the gloves, then press Enter to start' | Out-Null

$args = @(
    '-m', 'tools.hand_tonight', 'live',
    '--runs', (Join-Path $Work 'models/selected'),
    '--out', $out,
    '--hands', $Hands,
    '--seconds', $Seconds,
    '--threshold', '0.7',
    '--person', $Person,
    '--session', ("{0}_live_expected_{1}_{2}" -f $Person, $Gesture, $stamp)
)

$oldErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $python @args 2>&1 | Tee-Object -FilePath $consoleTemp
$exitCode = $LASTEXITCODE
$ErrorActionPreference = $oldErrorActionPreference

if (Test-Path -LiteralPath $out) {
    Move-Item -Force -LiteralPath $metadataTemp -Destination (Join-Path $out 'test_metadata.json')
    Move-Item -Force -LiteralPath $consoleTemp -Destination (Join-Path $out 'console.txt')
}
if ($exitCode -ne 0) {
    throw "Live test exited with code $exitCode. Keep the temporary console log at $consoleTemp and any runner output at $out"
}
Write-Host "Completed. Review predictions.jsonl and console.txt in: $out" -ForegroundColor Green
