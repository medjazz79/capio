param(
    [ValidateSet("reader", "float")]
    [string]$Mode = "reader",
    [switch]$SetupOnly
)

$ErrorActionPreference = "Stop"
$setup = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $setup
$venv = Join-Path $root ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
$venvPythonw = Join-Path $venv "Scripts\pythonw.exe"
$requirements = @(
    (Join-Path $setup "requirements.txt"),
    (Join-Path $setup "requirements-speech.txt")
)
$entryPoint = if ($Mode -eq "float") { "main_02.py" } else { "main.py" }
$script = Join-Path $root $entryPoint

function Start-Capio {
    Start-Process -FilePath $venvPythonw -ArgumentList @("`"$script`"") -WorkingDirectory $root
}

# A configured installation can start directly from its own environment.
# Avoid probing the system Python and importing the full dependency stack
# in a separate process on every launch. SetupOnly retains the full check.
$requirementsHash = (
    Get-FileHash -Algorithm SHA256 -LiteralPath $requirements |
        ForEach-Object { $_.Hash }
) -join ":"
$marker = Join-Path $venv ".harmony-requirements.sha256"
$installedHash = ""
if (Test-Path -LiteralPath $marker) {
    $installedHash = (Get-Content -LiteralPath $marker -Raw).Trim()
}
$dependencyFiles = @(
    "PySide6\__init__.py", "pymupdf\__init__.py", "requests\__init__.py",
    "PIL\__init__.py", "mammoth\__init__.py", "mss\__init__.py",
    "langdetect\__init__.py", "edge_tts\__init__.py"
)
$missingDependency = $dependencyFiles | Where-Object {
    -not (Test-Path -LiteralPath (Join-Path $venv "Lib\site-packages\$_"))
}
if (-not $SetupOnly -and $installedHash -eq $requirementsHash `
        -and (Test-Path -LiteralPath $venvPython) `
        -and (Test-Path -LiteralPath $venvPythonw) `
        -and -not $missingDependency) {
    Start-Capio
    exit 0
}

function Stop-WithGuidance([string]$message, [string]$url = "") {
    Write-Host ""
    Write-Host $message -ForegroundColor Red
    if ($url) {
        Write-Host $url -ForegroundColor Cyan
        try {
            Start-Process $url
        }
        catch {
            Write-Host "Open the link above in your browser."
        }
    }
    Write-Host ""
    Read-Host "Press Enter to close"
    exit 1
}

function Test-PythonVersion($command, [string[]]$prefix) {
    $versionText = & $command @prefix -c "import sys; print('%s.%s' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -ne 0) {
        return $null
    }
    try {
        return [version]($versionText | Select-Object -Last 1)
    }
    catch {
        return $null
    }
}

$pythonCommand = $null
$pythonPrefix = @()
$launcher = Get-Command "py.exe" -ErrorAction SilentlyContinue
if ($launcher) {
    $candidateVersion = Test-PythonVersion $launcher.Source @("-3")
    if ($candidateVersion) {
        $pythonCommand = $launcher.Source
        $pythonPrefix = @("-3")
    }
}

if (-not $pythonCommand) {
    $python = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if ($python) {
        $candidateVersion = Test-PythonVersion $python.Source @()
        if ($candidateVersion) {
            $pythonCommand = $python.Source
        }
    }
}

if (-not $pythonCommand) {
    Stop-WithGuidance `
        "Python 3.10 or newer is required. Install Python for Windows, enable the Python Launcher, then run this file again." `
        "https://www.python.org/downloads/windows/"
}
if ($candidateVersion -lt [version]"3.10") {
    Stop-WithGuidance `
        "Python 3.10 or newer is required. The detected version is $candidateVersion. Install a newer Python, then run this file again." `
        "https://www.python.org/downloads/windows/"
}

Write-Host "Capio setup" -ForegroundColor Cyan
Write-Host "Using Python $candidateVersion"

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Creating the private project environment..."
    & $pythonCommand @pythonPrefix -m venv $venv
    if ($LASTEXITCODE -ne 0) {
        Stop-WithGuidance "Could not create the project Python environment."
    }
}

$dependenciesWork = $false
if ($installedHash -eq $requirementsHash) {
    & $venvPython -c "import PySide6, pymupdf, requests, PIL, mammoth, mss, langdetect, edge_tts" 2>$null
    $dependenciesWork = ($LASTEXITCODE -eq 0)
}

if (-not $dependenciesWork) {
    Write-Host "Downloading and installing required libraries. This can take several minutes..." -ForegroundColor Yellow
    & $venvPython -m pip install -r $requirements[0] -r $requirements[1]
    if ($LASTEXITCODE -ne 0) {
        Stop-WithGuidance "Library installation failed. Check your internet connection and try again."
    }
    Set-Content -LiteralPath $marker -Value $requirementsHash -Encoding ASCII
}
else {
    Write-Host "Required libraries are already installed."
}

if ($SetupOnly) {
    Write-Host "Setup check completed successfully." -ForegroundColor Green
    exit 0
}

Write-Host "Starting $entryPoint..."
Start-Capio
