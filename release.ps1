# Publishes a new version for the app's "update" button.
#   1. Raise VERSION in app\version.py (e.g. 1.0.0 -> 1.1.0).
#   2. Run:  .\release.ps1 "What's new, one short line (shown to the user in Russian)"
param([Parameter(Mandatory = $true)][string]$Notes)
# Failures are checked via $LASTEXITCODE: with "Stop", PowerShell 5.1 would also abort on the
# progress messages git and gh print to stderr.
Set-Location $PSScriptRoot

$version = [regex]::Match((Get-Content app\version.py -Raw), 'VERSION = "([^"]+)"').Groups[1].Value
if (-not $version) { throw "VERSION not found in app\version.py" }
if ((Get-Content app\version.py -Raw) -match 'UPDATE_REPO = ""') { throw "Set UPDATE_REPO in app\version.py first" }

& "$PSScriptRoot\build.bat"
if ($LASTEXITCODE -ne 0) { throw "Build failed" }

git push
if ($LASTEXITCODE -ne 0) { throw "git push failed" }
gh release create "v$version" dist\KindleBooks.exe --title "v$version" --notes $Notes
if ($LASTEXITCODE -ne 0) { throw "gh release failed" }
Write-Host "Published v$version"
