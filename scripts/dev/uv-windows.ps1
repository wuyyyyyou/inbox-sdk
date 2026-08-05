$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

# Windows and WSL virtual environments contain platform-specific executables.
$env:UV_PROJECT_ENVIRONMENT = Join-Path $projectRoot "inbox-tool\src\.venv-windows"
if (-not $env:UV_LINK_MODE) {
    $env:UV_LINK_MODE = "copy"
}

& uv --directory (Join-Path $projectRoot "inbox-tool\src") @args
exit $LASTEXITCODE
