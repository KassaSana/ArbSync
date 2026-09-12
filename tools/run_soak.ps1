<#
.SYNOPSIS
    Runs an ArbSync backend and the live soak observer for a fixed duration.

.DESCRIPTION
    Starts the backend from the project virtualenv, resolves the process id the
    observer must sample, blocks system sleep for the duration of the run, waits
    for readiness, then runs tools/soak.py. The backend is stopped and
    sleep is re-enabled on exit, including when the run is interrupted.

    Resolving the process id matters: the virtualenv launcher re-execs the base
    interpreter as a child process, so the id Start-Process returns belongs to a
    few-megabyte stub. Sampling it would report a flat, meaningless RSS instead
    of the backend's real memory use.

    The observer checkpoints its report after every sample, so an interrupted
    run still leaves a readable report marked "in progress".

.EXAMPLE
    .\tools\run_soak.ps1
    Runs the full 24-hour soak with 60-second samples.

.EXAMPLE
    .\tools\run_soak.ps1 -DurationSeconds 300 -SampleSeconds 10
    Runs a short smoke test.
#>
[CmdletBinding()]
param(
    [double] $DurationSeconds = 86400,
    [double] $SampleSeconds = 60,
    [string] $BaseUrl = "http://127.0.0.1:8000",
    [string] $Output,
    [string] $Config,
    [int] $ReadyTimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not $Config) { $Config = Join-Path $repoRoot "config.toml" }
$Config = (Resolve-Path -LiteralPath $Config).Path
if (-not (Test-Path $python)) {
    throw "Project virtualenv not found at $python. Run 'uv sync --locked --extra dev' first."
}

function Resolve-BackendPid {
    # Walk down to the deepest python.exe descendant, which is the interpreter
    # actually running arb.main.
    param([int] $StartPid)

    $current = $StartPid
    while ($true) {
        $child = Get-CimInstance Win32_Process -Filter "ParentProcessId = $current AND Name = 'python.exe'" |
            Select-Object -First 1
        if (-not $child) { break }
        $current = [int] $child.ProcessId
    }
    return $current
}

$stamp = (Get-Date).ToString("yyyy-MM-dd_HHmmss")
if (-not $Output) {
    $Output = Join-Path $repoRoot "artifacts\benchmarks\soak\soak_24h_$stamp.md"
}
$outputDir = Split-Path -Parent $Output
if ($outputDir) {
    New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
}
$logDir = Join-Path $repoRoot "var\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stdoutLog = Join-Path $logDir "soak_backend_$stamp.log"
$stderrLog = Join-Path $logDir "soak_backend_$stamp.err.log"

# ES_CONTINUOUS keeps the request active until cleared; ES_SYSTEM_REQUIRED stops
# the machine from sleeping. The display is left free to turn off.
Add-Type -Namespace ArbSoak -Name Power -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError = true)]
public static extern uint SetThreadExecutionState(uint esFlags);
'@
$esContinuous = [uint32] 2147483648
$esSystemRequired = [uint32] 1

$backend = $null
try {
    if ([ArbSoak.Power]::SetThreadExecutionState($esContinuous -bor $esSystemRequired) -eq 0) {
        Write-Warning "Could not block system sleep; the run may be interrupted."
    }

    Write-Host "Starting backend from $python"
    $backend = Start-Process -FilePath $python -ArgumentList "-m", "arb.main", "--config", ('"{0}"' -f $Config) `
        -WorkingDirectory $repoRoot -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
    Write-Host "Backend pid: $($backend.Id)"

    $deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        if ($backend.HasExited) {
            throw "Backend exited during startup with code $($backend.ExitCode). See $stderrLog."
        }
        try {
            $response = Invoke-WebRequest -Uri "$BaseUrl/readyz" -UseBasicParsing -TimeoutSec 5
            if (($response.Content | ConvertFrom-Json).status -eq "ready") {
                $ready = $true
                break
            }
        }
        catch {
            # /readyz answers 503 until every adapter has a trusted book.
        }
        Start-Sleep -Seconds 2
    }
    if (-not $ready) {
        throw "Backend was not ready within $ReadyTimeoutSeconds seconds. See $stderrLog."
    }

    $samplePid = Resolve-BackendPid -StartPid $backend.Id
    if ($samplePid -ne $backend.Id) {
        Write-Host "Sampling child interpreter pid: $samplePid"
    }

    Write-Host "Backend ready. Observing for $DurationSeconds seconds into $Output"
    & $python (Join-Path $repoRoot "tools\soak.py") `
        --base-url $BaseUrl `
        --duration-seconds $DurationSeconds `
        --sample-seconds $SampleSeconds `
        --pid $samplePid `
        --output $Output
    if ($LASTEXITCODE -ne 0) {
        throw "Observer exited with code $LASTEXITCODE."
    }
}
finally {
    if ($backend) {
        # The stub and the interpreter it re-execed must both go, or the backend
        # keeps holding the port after this script exits.
        Write-Host "Stopping backend pid $($backend.Id)"
        foreach ($target in @((Resolve-BackendPid -StartPid $backend.Id), $backend.Id)) {
            Stop-Process -Id $target -Force -ErrorAction SilentlyContinue
        }
    }
    [void][ArbSoak.Power]::SetThreadExecutionState($esContinuous)
}
