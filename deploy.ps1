# Wdrożenie Glovaro na GitHub (Render zwykle deployuje z main)
Set-Location $PSScriptRoot
$ErrorActionPreference = "Stop"

git add app.py templates/rezerwacja_form.html templates/salon.html .env.example deploy.ps1
git status
git diff --cached --stat

git commit -m "Lista uslug bez zera - oczyszczanie nazw i build id"
git push origin main

Write-Host ""
Write-Host "Po pushu: Render -> Manual Deploy. Sprawdz: https://glovaro.pl/health (pole build)"
Write-Host "Potem odswiez rezerwacje (Ctrl+F5)."
