param(
    [string]$Model = 'qwen3:8b',
    [ValidateRange(1024, 65535)][int]$Port = 8000,
    [ValidateRange(2048, 131072)][int]$NumCtx = 16384,
    [ValidateRange(1, 3600)][int]$ModelTimeout = 600,
    [switch]$DownloadModel,
    [switch]$RestartOwnedApp
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $projectRoot '.local\runtime'
$ollamaExe = Join-Path $projectRoot '.local\ollama\ollama.exe'
$modelsDir = Join-Path $projectRoot '.local\ollama-models'
$appBase = "http://127.0.0.1:$Port"
$ollamaBase = 'http://127.0.0.1:11434'
New-Item -ItemType Directory -Force -Path $runtimeDir, $modelsDir | Out-Null
$pythonExe = (Get-Command python -ErrorAction Stop).Source

function Read-LocalJson([string]$Uri) {
    try { return Invoke-RestMethod -Uri $Uri -TimeoutSec 3 } catch { return $null }
}

function Wait-LocalJson([string]$Uri, [int]$Seconds = 30) {
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    do {
        $value = Read-LocalJson $Uri
        if ($null -ne $value) { return $value }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Service did not become ready: $Uri. Inspect logs in $runtimeDir."
}

function Stop-OwnedApp {
    $pidPath = Join-Path $runtimeDir 'app-pid.txt'
    if (-not (Test-Path -LiteralPath $pidPath)) {
        throw 'No owned app PID file. Stop the existing server yourself or choose another -Port.'
    }
    $appPidValue = 0
    if (-not [int]::TryParse((Get-Content -LiteralPath $pidPath -Raw).Trim(), [ref]$appPidValue)) {
        throw 'Invalid app PID file; refusing to stop an unknown process.'
    }
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $appPidValue" -ErrorAction SilentlyContinue
    $pidStamp = (Get-Item -LiteralPath $pidPath).LastWriteTimeUtc
    if ($null -eq $processInfo -or
        $processInfo.ExecutablePath -ine $pythonExe -or
        $processInfo.CommandLine -notmatch '(?i)(?:^|\s)-m\s+server\.app(?:\s|$)' -or
        [Math]::Abs(($processInfo.CreationDate.ToUniversalTime() - $pidStamp).TotalSeconds) -gt 30) {
        throw 'PID ownership could not be verified. Stop the existing server yourself or use another -Port.'
    }
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not ($listener | Where-Object { $_.OwningProcess -eq $appPidValue })) {
        throw 'Owned PID does not own the requested listening port; refusing to stop it.'
    }
    Stop-Process -Id $appPidValue -ErrorAction Stop
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while (Get-Process -Id $appPidValue -ErrorAction SilentlyContinue) {
        if ([DateTime]::UtcNow -gt $deadline) { throw 'Owned app did not stop within 10 seconds.' }
        Start-Sleep -Milliseconds 200
    }
}

$lock = $null
try {
    try {
        $lock = [IO.File]::Open((Join-Path $runtimeDir 'start-app.lock'), [IO.FileMode]::OpenOrCreate,
            [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch { throw 'Another start-app invocation is already running.' }

    $tags = Read-LocalJson "$ollamaBase/api/tags"
    if ($null -eq $tags) {
        if (-not (Test-Path -LiteralPath $ollamaExe)) {
            throw "Portable Ollama not found at $ollamaExe. Install the official portable distribution there."
        }
        if (Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue) {
            throw 'Port 11434 is occupied but is not responding as Ollama; no duplicate service started.'
        }
        $env:OLLAMA_MODELS = $modelsDir
        $env:OLLAMA_HOST = '127.0.0.1:11434'
        if (-not $env:OLLAMA_FLASH_ATTENTION) { $env:OLLAMA_FLASH_ATTENTION = '1' }
        if (-not $env:OLLAMA_KV_CACHE_TYPE) { $env:OLLAMA_KV_CACHE_TYPE = 'q8_0' }
        if (-not $env:OLLAMA_NUM_PARALLEL) { $env:OLLAMA_NUM_PARALLEL = '1' }
        $env:CQA_FLASH_ATTENTION = $env:OLLAMA_FLASH_ATTENTION
        $env:CQA_KV_CACHE_TYPE = $env:OLLAMA_KV_CACHE_TYPE
        $ollamaProcess = Start-Process -FilePath $ollamaExe -ArgumentList 'serve' -WorkingDirectory $projectRoot `
            -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeDir 'ollama.stdout.log') `
            -RedirectStandardError (Join-Path $runtimeDir 'ollama.stderr.log') -PassThru
        $ollamaProcess.Id | Set-Content -LiteralPath (Join-Path $runtimeDir 'ollama-pid.txt')
        $tags = Wait-LocalJson "$ollamaBase/api/tags" 30
        Write-Output "Started local Ollama, PID $($ollamaProcess.Id). Models: $modelsDir"
    } else {
        Write-Output 'Reusing existing local Ollama; its model-storage path is controlled by its original launch.'
    }

    $names = @($tags.models | ForEach-Object { $_.name })
    if ($names -notcontains $Model) {
        if (-not $DownloadModel) {
            throw "Model '$Model' is not installed. Rerun with -DownloadModel to explicitly download it into the running Ollama service's storage."
        }
        if (-not (Test-Path -LiteralPath $ollamaExe)) { throw 'Portable Ollama CLI is required for -DownloadModel.' }
        $env:OLLAMA_MODELS = $modelsDir
        $env:OLLAMA_HOST = '127.0.0.1:11434'
        & $ollamaExe pull $Model
        if ($LASTEXITCODE -ne 0) { throw 'Ollama model download failed. Existing app was not stopped.' }
        $tags = Read-LocalJson "$ollamaBase/api/tags"
        if (@($tags.models | ForEach-Object { $_.name }) -notcontains $Model) {
            throw 'Downloaded model is not listed by Ollama; application was not started.'
        }
    }

    $health = Read-LocalJson "$appBase/api/health"
    if ($null -ne $health) {
        if ($health.status -ne 'ok' -or $health.api_version -ne 1) { throw 'Port is occupied by an unexpected application.' }
        if ($RestartOwnedApp) {
            Stop-OwnedApp
        } elseif ($health.model_configured) {
            Write-Output "Existing CQA app is configured at $appBase/. Its existing model/context settings are unchanged."
            Write-Output 'This is a readiness check, not proof of real inference. Use python -m server.model_check with matching environment.'
            return
        } else {
            throw 'Existing CQA app has no model configured. Rerun with -RestartOwnedApp to restart only a verified owned process, or stop it yourself.'
        }
    } elseif (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
        throw "Port $Port is occupied but is not responding as CQA. No duplicate app was started."
    }

    if ([string]::IsNullOrWhiteSpace($env:CQA_THINK)) { $env:CQA_THINK = '1' }
    $env:CQA_MODEL = $Model
    $env:CQA_OLLAMA_URL = $ollamaBase
    $env:CQA_NUM_CTX = [string]$NumCtx
    $env:CQA_MODEL_TIMEOUT = [string]$ModelTimeout
    $appProcess = Start-Process -FilePath $pythonExe -ArgumentList '-m', 'server.app', '--port', $Port `
        -WorkingDirectory $projectRoot -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $runtimeDir 'app.stdout.log') `
        -RedirectStandardError (Join-Path $runtimeDir 'app.stderr.log') -PassThru
    $appProcess.Id | Set-Content -LiteralPath (Join-Path $runtimeDir 'app-pid.txt')
    $health = Wait-LocalJson "$appBase/api/health" 30
    if ($health.status -ne 'ok' -or -not $health.model_configured) { throw 'CQA app did not report configured model readiness.' }
    if ($appProcess.HasExited) { throw 'New app exited; another service may own the port. Inspect app logs.' }
    @{ pid=$appProcess.Id; port=$Port; model=$Model; num_ctx=$NumCtx; timeout=$ModelTimeout;
       think=($env:CQA_THINK -eq '1'); started_at=[DateTime]::UtcNow.ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtimeDir 'app-launch.json')
    Write-Output "CQA ready: $appBase/ | PID $($appProcess.Id) | model $Model | context $NumCtx | timeout $ModelTimeout seconds"
    Write-Output 'Model exists and app is configured. Actual inference and semantic quality require a separate evaluation check.'
} finally {
    if ($null -ne $lock) { $lock.Dispose() }
}
