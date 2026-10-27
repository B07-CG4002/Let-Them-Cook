param(
    [Parameter(Mandatory=$true)]
    [ValidateSet("Validate", "Build")]
    [string]$Mode,
    [Parameter(Mandatory=$true)]
    [string]$Python,
    [string]$Vivado = "C:\AMDDesignTools\2026.1\Vivado\bin\vivado.bat",
    [ValidateRange(1,16)]
    [int]$Jobs = 4
)
$ErrorActionPreference = "Stop"
$experiment = [System.IO.Directory]::GetParent($PSScriptRoot).FullName
$Python = (Resolve-Path -LiteralPath $Python -ErrorAction Stop).Path
if (!(Test-Path -LiteralPath $Vivado -PathType Leaf)) {
    throw "Vivado launcher missing: $Vivado"
}
$checker = Join-Path $PSScriptRoot "hand_vivado_prepare.py"
& $Python $checker verify --experiment $experiment --phase $Mode
if ($LASTEXITCODE -ne 0) { throw "Build preflight failed." }
$script = "setup.tcl"
if ($Mode -eq "Build") { $script = "build.tcl" }
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$log = Join-Path $experiment ($Mode.ToLower() + "_" + $stamp + "_console.log")
$vivadoLog = Join-Path $experiment ($Mode.ToLower() + "_" + $stamp + "_vivado.log")
$journal = Join-Path $experiment ($Mode.ToLower() + "_" + $stamp + ".jou")
Write-Host "Vivado $Mode is running. Full output is saved here:"
Write-Host $log
Write-Host "Wait for RUN FINISHED or an error. Keep this terminal and laptop running."
Push-Location $experiment
try {
    $savedPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $Vivado -mode batch -notrace -log $vivadoLog -journal $journal -source (Join-Path $PSScriptRoot $script) -tclargs $experiment $Jobs *> $log
    $result = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $savedPreference
    Pop-Location
}
Get-Content -LiteralPath $log -Tail 35
if ($result -ne 0) { throw "Vivado $Mode failed. Send the final output and log: $log" }
if ($Mode -eq "Validate") {
    & $Python $checker record-validation --experiment $experiment
}
else {
    & $Python $checker finalize --experiment $experiment
}
if ($LASTEXITCODE -ne 0) { throw "Result verification stopped. Send the output above." }
Write-Host "RUN FINISHED: $Mode completed. No FPGA was programmed."
