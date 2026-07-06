Param(
  [Parameter(Mandatory=$true)]
  [string]$RemoteUrl
)

Write-Host "Initializing git repository..."
git init
git add .
git commit -m "Initial trading bot setup"

Write-Host "Adding remote: $RemoteUrl"
git remote add origin $RemoteUrl

Write-Host "Creating main branch and pushing..."
git branch -M main
git push -u origin main

Write-Host "Done. Repository pushed to $RemoteUrl"