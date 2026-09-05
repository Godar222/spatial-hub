$ErrorActionPreference = 'Stop'
$repoRaw = 'https://raw.githubusercontent.com/Godar222/spatial-hub/main'
$root = Join-Path $env:LOCALAPPDATA 'SpatialHub'
$bootstrap = Join-Path $root 'bootstrap'
$launcher = Join-Path $bootstrap 'launcher.pyw'
Write-Host ''
Write-Host 'Spatial Hub for Windows' -ForegroundColor Cyan
Write-Host 'Installing bootstrap and automatic updater...' -ForegroundColor Gray
$python = $null
try { $python = (& python -c "import sys; print(sys.executable)" 2>$null).Trim() } catch {}
if (-not $python -or -not (Test-Path $python)) { throw 'Python was not found. Install Python 3.11+ and run this installer again.' }
Write-Host ((& $python --version 2>&1 | Out-String).Trim())
New-Item -ItemType Directory -Force -Path $bootstrap | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $root 'app') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $root 'data') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $root 'logs') | Out-Null
Invoke-WebRequest -UseBasicParsing -Uri "$repoRaw/bootstrap/launcher.pyw?ts=$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())" -OutFile $launcher
$pythonw = Join-Path (Split-Path $python -Parent) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }
$shell = New-Object -ComObject WScript.Shell
function Make-Shortcut([string]$path) {
  $shortcut = $shell.CreateShortcut($path)
  $shortcut.TargetPath = $pythonw
  $shortcut.Arguments = "`"$launcher`""
  $shortcut.WorkingDirectory = $root
  $shortcut.Description = 'Spatial Hub'
  $shortcut.Save()
}
$desktop = [Environment]::GetFolderPath('Desktop')
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
Make-Shortcut (Join-Path $desktop 'Spatial Hub.lnk')
Make-Shortcut (Join-Path $startMenu 'Spatial Hub.lnk')
Make-Shortcut (Join-Path $startup 'Spatial Hub.lnk')
Write-Host 'Bootstrap installed.' -ForegroundColor Green
Write-Host 'If Windows Firewall asks, allow Python on PRIVATE networks only.' -ForegroundColor Yellow
Start-Process -FilePath $pythonw -ArgumentList "`"$launcher`""
Write-Host 'Done. Spatial Hub will auto-start after Windows sign-in.' -ForegroundColor Green
Start-Sleep -Seconds 2
