param(
 [Parameter(Mandatory=$true)]
 [ValidateSet('Audit','Prepare','Train','Validation','Live','Export','EvaluateTest','Guided','Edges')][string]$Step,
 [string]$Person='P01', [string]$Date='20261031',
 [string]$Campaign='repaired_drop_g2000_v1',
 [string]$Work='.comms-local/hand_repaired_drop_g2000_v1_candidate1',
 [ValidateSet(20,30)][int]$TrainTarget=30,
 [ValidateSet('train','validation','test')][string]$Stage='train',
 [switch]$AllowTestUse
)
$ErrorActionPreference='Stop'
$python='.\.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $python)) {throw 'Run from repository root.'}
$raw="data/hand_raw_flex3/$Campaign"
function Run-Python {
 & $python @args
 if ($LASTEXITCODE -ne 0) {throw "Command failed (exit $LASTEXITCODE). Stop and review."}
}
switch($Step) {
 'Audit' {
  Run-Python -m tools.hand_capture_review --root $raw --train-target $TrainTarget
  Run-Python -m tools.hand_tonight progress --input $raw
 }
 'Prepare' {Run-Python -m tools.hand_tonight prepare --input $raw --out "$Work/prepared"}
 'Train' {Run-Python -m tools.hand_tonight train --data "$Work/prepared" --out "$Work/models"}
 'Validation' {Run-Python -m tools.hand_tonight evaluate --runs "$Work/models/selected" --data "$Work/prepared" --split validation --out "$Work/validation.json"}
 'Live' {
  $out="$Work/live_$(Get-Date -Format yyyyMMdd_HHmmss_fff)"
  Run-Python -m tools.hand_tonight live --runs "$Work/models/selected" --hands BOTH --seconds 180 --out $out
 }
 'Export' {Run-Python -m tools.hand_tonight export-fpga --runs "$Work/models/selected" --data "$Work/prepared" --out "$Work/fpga_bundle"}
 'EvaluateTest' {
  if (!$AllowTestUse) {throw 'Freeze model/filter choices first, then explicitly pass -AllowTestUse.'}
  Run-Python -m tools.hand_tonight evaluate --runs "$Work/models/selected" --data "$Work/prepared" --split test --out "$Work/heldout_test.json" --allow-test-use
 }
 'Guided' {
  if ($Campaign -eq 'repaired_drop_g2000_v1') {throw 'Use -Campaign repaired_drop_g2000_guided_v2 for NEW collection. Existing completed data stay in v1.'}
  Run-Python -m tools.hand_guided_collection --person $Person --date $Date --campaign $Campaign --train-target $TrainTarget --stage $Stage
 }
 'Edges' {
  Run-Python -m tools.hand_collection --gyro-dps 2000 --person $Person --date $Date --campaign $Campaign --output $raw --edges ".comms-local/hand_edges/$Campaign" --stage diagnostic --only
 }
}
