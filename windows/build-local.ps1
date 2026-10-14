$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = (Resolve-Path (Join-Path $scriptDir '..')).Path
$ffmpegUrl = if ($env:TOPO_FFMPEG_URL) {
  $env:TOPO_FFMPEG_URL
} else {
  'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
}

function Invoke-Checked {
  param(
    [Parameter(Mandatory = $true)][string]$FilePath,
    [Parameter(Mandatory = $false)][string[]]$ArgumentList = @()
  )
  & $FilePath @ArgumentList
  if ($LASTEXITCODE -ne 0) {
    throw "$FilePath exited with code $LASTEXITCODE"
  }
}

$python = $null
$pythonArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
  & py -3.12 --version *> $null
  if ($LASTEXITCODE -eq 0) {
    $python = 'py'
    $pythonArgs = @('-3.12')
  }
}
if (-not $python -and (Get-Command python -ErrorAction SilentlyContinue)) {
  & python --version *> $null
  if ($LASTEXITCODE -eq 0) {
    $python = 'python'
  }
}
if (-not $python) {
  throw 'Python 3.12 is required. Install it from https://www.python.org/downloads/windows/ and retry.'
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
  throw 'Node.js and npm are required. Install the current LTS release from https://nodejs.org/ and retry.'
}

Write-Host 'Checking PyInstaller...'
& $python @pythonArgs -c 'import PyInstaller' *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host 'PyInstaller is missing; installing it for the current Python user...'
  Invoke-Checked $python ($pythonArgs + @('-m', 'pip', 'install', '--user', 'pyinstaller'))
}

$distDir = Join-Path $scriptDir 'dist'
$outDir = Join-Path $scriptDir 'out'
New-Item -ItemType Directory -Force -Path $distDir, $outDir | Out-Null

$ffmpegPath = Join-Path $distDir 'ffmpeg.exe'
if (-not (Test-Path $ffmpegPath)) {
  $temporaryDir = Join-Path ([System.IO.Path]::GetTempPath()) ("topo-ffmpeg-" + [guid]::NewGuid().ToString('N'))
  $archive = Join-Path $temporaryDir 'ffmpeg.zip'
  $extractDir = Join-Path $temporaryDir 'extract'
  try {
    New-Item -ItemType Directory -Force -Path $temporaryDir, $extractDir | Out-Null
    Write-Host 'Downloading the Windows FFmpeg runtime...'
    Invoke-WebRequest -Uri $ffmpegUrl -OutFile $archive
    Expand-Archive -Path $archive -DestinationPath $extractDir -Force
    $binary = Get-ChildItem -Path $extractDir -Filter ffmpeg.exe -Recurse | Select-Object -First 1
    if (-not $binary) {
      throw 'FFmpeg executable was not found in the downloaded archive.'
    }
    Copy-Item $binary.FullName $ffmpegPath -Force
  } finally {
    if (Test-Path $temporaryDir) {
      Remove-Item $temporaryDir -Recurse -Force -ErrorAction SilentlyContinue
    }
  }
}

Write-Host 'Building the Windows Python backend...'
$pyinstallerArgs = $pythonArgs + @(
  '-m', 'PyInstaller',
  '--clean', '--noconfirm', '--onefile', '--name', 'tapo-backend',
  '--add-data', "$(Join-Path $projectRoot 'cloud_web');cloud_web",
  '--add-data', "$(Join-Path $projectRoot 'certs');certs",
  '--add-data', "$(Join-Path $projectRoot 'data/io.github.filirnd.topo.png');.",
  '--distpath', $distDir,
  '--workpath', (Join-Path $scriptDir '.pyinstaller-build'),
  '--specpath', (Join-Path $scriptDir '.pyinstaller-spec'),
  (Join-Path $projectRoot 'tapo_cloud_app.py')
)
Invoke-Checked $python $pyinstallerArgs

Write-Host 'Installing Electron dependencies and building the NSIS installer...'
Push-Location $scriptDir
try {
  Invoke-Checked 'npm' @('ci', '--no-audit', '--no-fund')
  Invoke-Checked 'npm' @('run', 'dist')
} finally {
  Pop-Location
}

Write-Host "Installer files are in: $outDir"
