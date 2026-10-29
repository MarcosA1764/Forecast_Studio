param(
    [switch]$WithData,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path "index.html")) { throw "index.html not found; run this from the project root." }
if (-not (Test-Path "data\official.seed.json")) { throw "data\official.seed.json not found." }
if ($WithData -and -not (Test-Path "data\incoming.txt")) {
    throw "data\incoming.txt not found; omit -WithData or add the dataset."
}

$pyArgs = @(
    "-m", "PyInstaller",
    "--noconfirm", "--clean", "--onefile", "--console",
    "--name", "ForecastStudio",
    "--add-data", "index.html;.",
    "--add-data", "data/official.seed.json;data"
)
if ($WithData) {
    $pyArgs += @("--add-data", "data/incoming.txt;data")
}
$pyArgs += "server.py"

if ($DryRun) {
    Write-Host ("python " + ($pyArgs -join " "))
    exit 0
}

python @pyArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host ""
Write-Host "Built: dist\ForecastStudio.exe"
