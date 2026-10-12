Push-Location $PSScriptRoot

# Downloads the Bonsai release pinned in Bonsai.config (the version of the
# "Bonsai" package) and restores every other package from nuget.org. Nothing
# here needs a local package feed: Bonsai.ZeroMQ and Harp.Behavior are both
# public. Re-running is safe; the download is skipped once Bonsai.exe exists.

if (!(Test-Path "./Bonsai.exe")) {
    $release = "https://github.com/bonsai-rx/bonsai/releases/latest/download/Bonsai.zip"
    $configPath = "./Bonsai.config"
    if (Test-Path $configPath) {
        [xml]$config = Get-Content $configPath
        $bootstrapper = $config.PackageConfiguration.Packages.Package.where{$_.id -eq 'Bonsai'}
        if ($bootstrapper) {
            $version = $bootstrapper.version
            $release = "https://github.com/bonsai-rx/bonsai/releases/download/$version/Bonsai.zip"
        }
    }
    Write-Host "Downloading Bonsai..."
    Invoke-WebRequest $release -OutFile "temp.zip"
    Move-Item -Path "NuGet.config" "temp.config" -ErrorAction SilentlyContinue
    Expand-Archive "temp.zip" -DestinationPath "." -Force
    Move-Item -Path "temp.config" "NuGet.config" -Force -ErrorAction SilentlyContinue
    Remove-Item -Path "temp.zip"
}

Write-Host "Restoring Bonsai packages..."
& .\Bonsai.exe --no-editor

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "Setup complete. Open Bonsai, then File -> Open -> Zapit.bonsai"
    Write-Host "Set PortName and ZapitAddress in the Properties panel before starting the workflow."
}
else {
    Write-Warning "Bonsai exited with code $LASTEXITCODE. The package restore may not have completed."
}

Pop-Location
