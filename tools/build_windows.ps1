param(
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if (-not $PythonExe) {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $PythonExe = (& $launcher.Source -3.12 -c "import sys; print(sys.executable)").Trim()
    } else {
        $PythonExe = (Get-Command python.exe -ErrorAction Stop).Source
    }
}
$PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path
$pythonRoot = Split-Path -Parent $PythonExe
$windowsRoot = [Environment]::GetFolderPath("Windows")

$env:PATH = @(
    $pythonRoot
    (Join-Path $pythonRoot "Scripts")
    $windowsRoot
    (Join-Path $windowsRoot "System32")
    (Join-Path $windowsRoot "System32\Wbem")
) -join ";"
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
Remove-Item Env:QT_PLUGIN_PATH -ErrorAction SilentlyContinue
Remove-Item Env:QML2_IMPORT_PATH -ErrorAction SilentlyContinue

$distPath = Join-Path $projectRoot "build\dist-release"
$workPath = Join-Path $projectRoot "build\pyinstaller-release"
$specPath = Join-Path $projectRoot "SanaDeclarationUploader.spec"

& $PythonExe -m PyInstaller `
    --noconfirm `
    --clean `
    --distpath $distPath `
    --workpath $workPath `
    $specPath
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE."
}

$analysisPath = Join-Path $workPath "SanaDeclarationUploader\Analysis-00.toc"
if (Select-String -LiteralPath $analysisPath -SimpleMatch "codex-runtimes" -Quiet) {
    throw "Build output contains DLLs from codex-runtimes."
}

$exePath = Join-Path $distPath "SanaDeclarationUploader\SanaDeclarationUploader.exe"
if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw "Expected executable was not created: $exePath"
}

Write-Output $exePath
