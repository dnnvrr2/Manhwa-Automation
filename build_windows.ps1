$ErrorActionPreference = 'Stop'
if (-not (Test-Path 'assets/icons')) { throw 'assets/icons is required for the application control icons.' }
$assetArgs = @()
if (Test-Path 'assets') { $assetArgs = @('--add-data', 'assets;assets') }
python -m PyInstaller --noconfirm --clean --windowed --name ManhwaAutomation @assetArgs main.py
Write-Host "Built dist\ManhwaAutomation\ManhwaAutomation.exe"
