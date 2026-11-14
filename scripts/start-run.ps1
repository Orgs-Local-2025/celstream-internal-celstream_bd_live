<#
.SYNOPSIS
  (BD operator, on the hosting workstation) Queue a workflow run while the shared portal is
  read-only. Progress is visible to everyone in the portal's "Live execution" tab.

.EXAMPLE
  powershell -File scripts\start-run.ps1 -MaxJobs 3 -SyncExisting      # new jobs + retry incomplete
  powershell -File scripts\start-run.ps1 -Reprocess                     # re-check stored jobs, no new CRM records
  powershell -File scripts\start-run.ps1 -Status
#>
param([int]$MaxJobs = 1, [switch]$SyncExisting, [switch]$Reprocess, [string[]]$JobId = @(),
      [switch]$Status)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
if ($Status) { docker compose exec -T bd-live-worker python -m bdlive.runs status; exit $LASTEXITCODE }
$cliArgs = @('python', '-m', 'bdlive.runs', 'request', '--max-jobs', $MaxJobs, '--source',
             "operator:$env:USERNAME")
if ($SyncExisting) { $cliArgs += '--sync-existing' }
if ($Reprocess) { $cliArgs += '--reprocess' }
foreach ($id in $JobId) { $cliArgs += @('--job-id', $id) }
docker compose exec -T bd-live-worker @cliArgs
exit $LASTEXITCODE
