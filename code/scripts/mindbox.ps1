[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'restart', 'reset', 'stop', 'status')]
    [string]$Action = 'start'
)

$ErrorActionPreference = 'Stop'
$codeRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $codeRoot 'backend'
$deviceDir = Join-Path $codeRoot 'device\k230'
$webDir = Join-Path $codeRoot 'webui'
$pythonExe = Join-Path $backendDir '.venv\Scripts\python.exe'
$logDir = Join-Path $backendDir 'var\log'

function Read-Port($Name, $Fallback) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if (-not $value) { return $Fallback }
    $portValue = 0
    if (-not [int]::TryParse($value, [ref]$portValue) -or $portValue -lt 1 -or $portValue -gt 65535) {
        throw "Invalid port in $Name"
    }
    return $portValue
}

$bridgePort = Read-Port 'MIND_BOX_BRIDGE_PORT' 8765
$backendPort = Read-Port 'MIND_BOX_BACKEND_PORT' 8787
$webPort = Read-Port 'MIND_BOX_WEB_PORT' 5173
$agentPort = Read-Port 'MIND_BOX_AGENT_PORT' 8788
if (@(@($bridgePort, $backendPort, $webPort, $agentPort) | Select-Object -Unique).Count -ne 4) {
    throw 'Bridge, backend, web and Agent API ports must be different.'
}
$services = @(
    @{ Name = 'bridge'; Port = $bridgePort; Directory = $deviceDir; Marker = 'pc_door_bridge.py';
       Probe = '/api/camera/state'; File = $pythonExe;
       Arguments = @('-u', (Join-Path $deviceDir 'pc_door_bridge.py'), '--port', "$bridgePort", '--door-source', 'vision', '--record-audio') },
    @{ Name = 'backend'; Port = $backendPort; Directory = $backendDir; Marker = '-m app.server';
       Probe = '/api/voice/health'; File = $pythonExe;
       Arguments = @('-m', 'app.server', '--port', "$backendPort", '--bridge', "http://127.0.0.1:$bridgePort") },
    @{ Name = 'web'; Port = $webPort; Directory = $webDir; Marker = 'vite'; Probe = '/';
       File = $null; Arguments = @((Join-Path $webDir 'node_modules\vite\bin\vite.js'), '--host', '127.0.0.1', '--port', "$webPort", '--strictPort') },
    @{ Name = 'agent'; Port = $agentPort; Directory = $backendDir; Marker = '-m app.agent_server';
       Probe = '/health'; File = $pythonExe; Arguments = @('-m', 'app.agent_server', '--port', "$agentPort") }
)

function Get-Listeners($Service) {
    $ids = @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object { $_.LocalPort -eq $Service.Port } |
        Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($processId in $ids) {
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $processId"
        if ($processInfo) { $processInfo }
    }
}

function Assert-Owned($Service, $Processes) {
    foreach ($processInfo in $Processes) {
        $command = [string]$processInfo.CommandLine
        $normalized = $command.Replace('/', '\')
        if (-not $normalized.ToLowerInvariant().Contains($Service.Directory.ToLowerInvariant()) -or
            -not $command.Replace('"', '').Contains($Service.Marker)) {
            throw "Port $($Service.Port) belongs to another program (PID $($processInfo.ProcessId)); it was not stopped."
        }
    }
}

function Test-Ready($Service) {
    try {
        $url = "http://127.0.0.1:$($Service.Port)$($Service.Probe)"
        $response = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 1
        if ($response.StatusCode -ne 200) { return $false }
        if ($Service.Name -in @('backend', 'agent')) { return ($response.Content | ConvertFrom-Json).ok -eq $true }
        if ($Service.Name -eq 'bridge') { return $null -ne ($response.Content | ConvertFrom-Json).connected }
        return $response.Content.IndexOf('mind box', [StringComparison]::OrdinalIgnoreCase) -ge 0
    } catch { return $false }
}

function Stop-Services {
    $owned = @()
    foreach ($service in $services) {
        $listeners = @(Get-Listeners $service)
        Assert-Owned $service $listeners
        $owned += $listeners
    }
    foreach ($processInfo in $owned) {
        $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($processInfo.ProcessId)"
        if ($current -and $current.CreationDate -eq $processInfo.CreationDate) {
            Write-Host "[mindbox] Stopping PID $($current.ProcessId)"
            Stop-Process -Id $current.ProcessId -ErrorAction Stop
        }
    }
    Write-Host '[mindbox] Services stopped. Database, recordings and model cache were kept.'
}

function Start-Services {
    if (-not (Test-Path -LiteralPath $pythonExe)) { throw "Missing Python environment: $pythonExe" }
    if (-not (Test-Path -LiteralPath $services[2].Arguments[0])) { throw 'Missing WebUI dependencies. Run npm install in code/webui.' }
    $services[2].File = (Get-Command node -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $check = 'import cv2, sounddevice; from app.server import create_app; from app.config import Settings; from app.voice_api import LocalTranscriber; assert cv2.aruco; Settings.load(); LocalTranscriber()._load(); print("Dependencies and local speech model ready")'
    Push-Location $backendDir
    try {
        $check | & $pythonExe -
        if ($LASTEXITCODE -ne 0) { throw 'Dependencies or speech model are missing; see code/backend/README.md.' }
    } finally { Pop-Location }

    # Check every occupied port before starting or replacing any process.
    foreach ($service in $services) {
        $listeners = @(Get-Listeners $service)
        Assert-Owned $service $listeners
        if ($listeners.Count -gt 0 -and -not (Test-Ready $service)) {
            throw "Existing $($service.Name) is not ready. Run this script with restart."
        }
    }
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $env:MIND_BOX_BRIDGE_URL = "http://127.0.0.1:$bridgePort"
    $env:MIND_BOX_BACKEND_URL = "http://127.0.0.1:$backendPort"
    $env:MIND_BOX_VOICE_URL = "http://127.0.0.1:$backendPort"
    $env:MIND_BOX_AGENT_PORT = "$agentPort"
    $env:MIND_BOX_OWNER_ORIGINS = "http://127.0.0.1:$webPort,http://localhost:$webPort"
    $env:PYTHONUTF8 = '1'
    $started = @()
    try {
        foreach ($service in $services) {
            if (@(Get-Listeners $service).Count -gt 0) {
                Write-Host "[mindbox] $($service.Name) already ready on port $($service.Port)"
                continue
            }
            $arguments = ($service.Arguments | ForEach-Object { '"' + $_ + '"' }) -join ' '
            $child = Start-Process -FilePath $service.File -ArgumentList $arguments -WorkingDirectory $service.Directory `
                -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDir "$($service.Name).stdout.log") `
                -RedirectStandardError (Join-Path $logDir "$($service.Name).stderr.log")
            $started += $child
            $deadline = [DateTime]::UtcNow.AddSeconds(25)
            while (-not (Test-Ready $service)) {
                if ($child.HasExited -or [DateTime]::UtcNow -gt $deadline) {
                    throw "$($service.Name) failed to start. See $logDir\$($service.Name).stderr.log"
                }
                Start-Sleep -Milliseconds 200
            }
            Write-Host "[mindbox] $($service.Name) ready on port $($service.Port)"
        }
    } catch {
        foreach ($child in $started) {
            if (-not $child.HasExited) { & taskkill.exe /PID $child.Id /T /F 2>&1 | Out-Null }
        }
        throw
    }
    Write-Host "[mindbox] WebUI: http://127.0.0.1:$webPort/"
    Write-Host "[mindbox] Logs: $logDir"
    Write-Host '[mindbox] Keep VS Code CanMV connected and run k230_key_door.py on the board.'
}

switch ($Action) {
    'status' {
        foreach ($service in $services) {
            $listeners = @(Get-Listeners $service)
            $state = if ($listeners.Count -eq 0) { 'stopped' } elseif (Test-Ready $service) { 'ready' } else { 'not ready' }
            Write-Host "[mindbox] $($service.Name) ($($service.Port)): $state"
        }
    }
    'stop' { Stop-Services }
    'start' { Start-Services }
    default { Stop-Services; Start-Services }
}
