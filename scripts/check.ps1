# Full local check (Windows). Same steps as CI. Stops at the first failure.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$steps = @(
    @("ruff", "format", "--check", "."),
    @("ruff", "check", "."),
    @("mypy"),
    @("lint-imports"),
    @("pytest")
)

foreach ($step in $steps) {
    Write-Host "==> uv run $($step -join ' ')" -ForegroundColor Cyan
    & uv run @step
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAILED: $($step -join ' ')" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}
Write-Host "All checks passed." -ForegroundColor Green
