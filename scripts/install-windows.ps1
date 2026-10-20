# Warraq installer for Windows 10/11
# - installs uv (a fast Python manager) if needed
# - creates .venv with Python 3.12
# - picks the right PyTorch build for your NVIDIA GPU (CUDA 13.0 / 12.6) or CPU
# - installs Warraq and creates Desktop + Start menu shortcuts
param(
  [ValidateSet("auto", "cu130", "cu126", "cpu")] [string]$Torch = "auto",
  [switch]$NoShortcuts,
  [switch]$DownloadModels
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Say($msg, $color = "Gray") { Write-Host "  $msg" -ForegroundColor $color }
Write-Host ""
Write-Host "  Warraq" -ForegroundColor Yellow
Write-Host "  Local Arabic OCR & translation" -ForegroundColor DarkGray
Write-Host ""

# ---------------------------------------------------------------- uv
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
  Say "Installing uv (Python manager)..."
  powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex" | Out-Null
  $env:Path = "$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$env:Path"
  $uv = Get-Command uv -ErrorAction SilentlyContinue
  if (-not $uv) { throw "uv could not be installed. Install it from https://docs.astral.sh/uv/ and run this again." }
}

# ---------------------------------------------------------------- Python env
if (-not (Test-Path ".venv\Scripts\python.exe")) {
  Say "Creating Python 3.12 environment..."
  uv venv --python 3.12 .venv | Out-Null
}
$py = Join-Path $Root ".venv\Scripts\python.exe"

# ---------------------------------------------------------------- PyTorch variant
$variant = $Torch
if ($variant -eq "auto") {
  $variant = "cpu"
  $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
  if (-not $smi -and (Test-Path "$env:SystemRoot\System32\nvidia-smi.exe")) { $smi = "$env:SystemRoot\System32\nvidia-smi.exe" }
  if ($smi) {
    try {
      $line = (& nvidia-smi --query-gpu=name,driver_version,compute_cap --format=csv,noheader | Select-Object -First 1)
      $parts = $line.Split(",") | ForEach-Object { $_.Trim() }
      $gpu = $parts[0]; $driver = [double]($parts[1].Split(".")[0]); $cc = [double]$parts[2]
      Say "Found $gpu (driver $($parts[1]), compute $($parts[2]))" "Green"
      if ($cc -lt 5.0) { Say "This GPU is too old for current PyTorch; using the CPU." "Yellow" }
      elseif ($driver -ge 580 -and $cc -ge 7.5) { $variant = "cu130" }
      elseif ($driver -ge 525) { $variant = "cu126" }
      else { Say "Your NVIDIA driver is old - update it from nvidia.com for GPU acceleration. Using the CPU for now." "Yellow" }
    } catch { Say "Could not query nvidia-smi; using the CPU build." "Yellow" }
  } else {
    Say "No NVIDIA GPU found - installing the CPU build (works everywhere, slower)." "Yellow"
  }
}
$index = "https://download.pytorch.org/whl/$variant"
Say "Installing PyTorch ($variant)... this is the big download (~2.5 GB for CUDA)."
uv pip install --python $py --upgrade torch torchvision --index-url $index
if ($LASTEXITCODE -ne 0) { throw "PyTorch installation failed." }

# ---------------------------------------------------------------- Warraq
Say "Installing Warraq..."
uv pip install --python $py -e .
if ($LASTEXITCODE -ne 0) { throw "Installing Warraq failed." }

& $py -c "import torch; print('  PyTorch', torch.__version__, '| CUDA available:', torch.cuda.is_available())"

# ---------------------------------------------------------------- shortcuts
if (-not $NoShortcuts) {
  $pyw = Join-Path $Root ".venv\Scripts\pythonw.exe"
  $icon = Join-Path $Root "warraq\assets\icon.ico"
  $shell = New-Object -ComObject WScript.Shell
  $targets = @([Environment]::GetFolderPath("Desktop"), (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"))
  foreach ($dir in $targets) {
    $lnk = $shell.CreateShortcut((Join-Path $dir "Warraq.lnk"))
    $lnk.TargetPath = $pyw
    $lnk.Arguments = "-m warraq"
    $lnk.WorkingDirectory = $Root
    $lnk.IconLocation = $icon
    $lnk.Description = "Warraq - Arabic OCR & translation"
    $lnk.Save()
  }
  Say "Shortcuts created on the Desktop and in the Start menu." "Green"
}

if ($DownloadModels) {
  Say "Downloading the OCR models (~2.3 GB)..."
  & $py -m warraq download
}

Write-Host ""
Say "All set. Start Warraq from the Desktop shortcut or run-windows.bat." "Green"
Write-Host ""
