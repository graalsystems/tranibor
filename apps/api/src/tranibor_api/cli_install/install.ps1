# SPDX-License-Identifier: Apache-2.0
#
# Installs the `tranibor` CLI from a Tranibor instance, in the version that instance runs.
#
#   irm __CHOREGOS_INSTANCE_URL__/cli/install.ps1 | iex
#
# Windows x86_64 (an ARM64 Windows runs it under emulation). It downloads the binary from the same
# instance, checks its sha256 against the instance's manifest, installs it as
# %LOCALAPPDATA%\Programs\tranibor\tranibor.exe and adds that folder to the user's PATH. No
# administrator rights. PowerShell 5.1 or later.
#
# For the window of ADR 0224 R5 the former command still works: `choregos.cmd` beside it runs
# tranibor.exe, and the `choregos.exe` an earlier install left in that folder or in the former default
# one (%LOCALAPPDATA%\Programs\choregos) is removed, so that `choregos` finds the new binary.
#
# Environment, read when set:
#   TRANIBOR_INSTALL_DIR   the folder to install into (default %LOCALAPPDATA%\Programs\tranibor)
#   TRANIBOR_URL           the instance to install from (default: the one that served this script)
#
# It stops with an error rather than `exit`: run through `iex`, `exit` would close the window.

function Install-Tranibor {
    [CmdletBinding()]
    param(
        [string]$InstanceUrl = '__CHOREGOS_INSTANCE_URL__',
        [string]$InstallDir = ''
    )
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    $releases = '__CHOREGOS_RELEASES__'
    # The former names (CHOREGOS_URL, CHOREGOS_INSTALL_DIR) still work (ADR 0176 R4).
    if ($env:TRANIBOR_URL) { $InstanceUrl = $env:TRANIBOR_URL } elseif ($env:CHOREGOS_URL) { $InstanceUrl = $env:CHOREGOS_URL }
    $InstanceUrl = $InstanceUrl.TrimEnd('/')
    if (-not $InstallDir) {
        if ($env:TRANIBOR_INSTALL_DIR) {
            $InstallDir = $env:TRANIBOR_INSTALL_DIR
        } elseif ($env:CHOREGOS_INSTALL_DIR) {
            $InstallDir = $env:CHOREGOS_INSTALL_DIR
        } else {
            $InstallDir = Join-Path $env:LOCALAPPDATA 'Programs\tranibor'
        }
    }

    # Windows PowerShell 5.1 may still offer TLS 1.0 first.
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $arch = $env:PROCESSOR_ARCHITEW6432
    if (-not $arch) { $arch = $env:PROCESSOR_ARCHITECTURE }
    if ($arch -ne 'AMD64' -and $arch -ne 'ARM64') {
        throw "tranibor install: unsupported processor $arch; the Windows CLI is built for x86_64"
    }
    $name = 'tranibor-windows-x86_64.exe'
    # An instance from before ADR 0224 R5 names its file `choregos-windows-x86_64.exe`: the same binary.
    $formerName = 'choregos-windows-x86_64.exe'

    $work = Join-Path ([IO.Path]::GetTempPath()) ('tranibor-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $work | Out-Null
    try {
        # The manifest: the version, and the checksum of this machine's binary.
        try {
            $manifest = Invoke-RestMethod -UseBasicParsing -Uri "$InstanceUrl/downloads/cli/manifest.json"
        } catch {
            throw "tranibor install: cannot read $InstanceUrl/downloads/cli/manifest.json ($($_.Exception.Message)); this instance may bundle no CLI, see $releases"
        }
        $entry = $manifest.files | Where-Object { $_.name -eq $name } | Select-Object -First 1
        if (-not $entry) {
            $entry = $manifest.files | Where-Object { $_.name -eq $formerName } | Select-Object -First 1
            if ($entry) { $name = $formerName }
        }
        if (-not $entry) {
            throw "tranibor install: this instance's CLI $($manifest.version) has no Windows binary ($name); see $releases"
        }
        $expected = ([string]$entry.sha256).ToLowerInvariant()
        if ($expected -notmatch '^[0-9a-f]{64}$') {
            throw "tranibor install: the manifest gives no valid sha256 for $name"
        }

        # The binary, verified before it goes anywhere.
        Write-Host "Downloading tranibor $($manifest.version) (windows/x86_64) from $InstanceUrl"
        $downloaded = Join-Path $work 'tranibor.exe'
        Invoke-WebRequest -UseBasicParsing -Uri "$InstanceUrl/downloads/cli/$name" -OutFile $downloaded
        # A managed Windows may refuse an executable that is new and not yet signed: Defender's attack
        # surface reduction rule 01443614-CD74-433A-B99E-2ECDC07BFC25 blocks files that do not meet a
        # prevalence, age or trusted-list criterion (event 1121 in Microsoft-Windows-Windows
        # Defender/Operational). Windows PowerShell 5.1 meets it here, reading the file; PowerShell 7
        # when it runs it. Installing it would leave a command that never runs.
        $refused = "On a managed Windows this is usually Defender's attack surface reduction rule for executables that are not yet widespread or signed (event 1121 in Microsoft-Windows-Windows Defender/Operational). Ask your IT administrator for an exclusion of $InstallDir, or install the CLI from its Python packages: $releases. Nothing was installed."
        try {
            $actual = (Get-FileHash -Algorithm SHA256 -Path $downloaded -ErrorAction Stop).Hash.ToLowerInvariant()
        } catch {
            throw "tranibor install: Windows refused access to the downloaded tranibor.exe ($($_.Exception.Message)). $refused"
        }
        if ($actual -ne $expected) {
            throw "tranibor install: checksum mismatch for ${name}: expected $expected, got $actual; nothing was installed"
        }

        # Run it once before it replaces anything (see $refused).
        try {
            $ran = & $downloaded --version 2>&1 | Out-String
            $code = $LASTEXITCODE
        } catch {
            throw "tranibor install: Windows refused to run the downloaded tranibor.exe ($($_.Exception.Message)). $refused"
        }
        if ($code -ne 0) {
            throw "tranibor install: the downloaded tranibor.exe exited with $code on --version ($($ran.Trim())); nothing was installed"
        }

        # A running tranibor.exe cannot be overwritten, but it can be renamed: the old one steps
        # aside, the new one takes its name, and the old one is removed when nothing holds it.
        New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
        $target = Join-Path $InstallDir 'tranibor.exe'
        $aside = "$target.old"
        if (Test-Path -LiteralPath $aside) { Remove-Item -LiteralPath $aside -Force -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $target) { Move-Item -LiteralPath $target -Destination $aside -Force }
        try {
            Move-Item -LiteralPath $downloaded -Destination $target -Force
        } catch {
            if (Test-Path -LiteralPath $aside) { Move-Item -LiteralPath $aside -Destination $target -Force }
            throw
        }
        if (Test-Path -LiteralPath $aside) { Remove-Item -LiteralPath $aside -Force -ErrorAction SilentlyContinue }
        Write-Host "Installed tranibor $($manifest.version) at $target"

        # The former command for the window of ADR 0224 R5: `choregos.cmd` runs tranibor.exe (no link:
        # a symbolic link needs a privilege a person may not have). An earlier install's choregos.exe
        # would win over it (.exe comes before .cmd in PATHEXT), here or in the former default folder
        # still on the PATH: it is renamed aside and removed, or removed at the next install if running.
        Set-Content -LiteralPath (Join-Path $InstallDir 'choregos.cmd') -Value "@`"%~dp0tranibor.exe`" %*" -Encoding Ascii
        $formerDirs = @($InstallDir, (Join-Path $env:LOCALAPPDATA 'Programs\choregos')) | Select-Object -Unique
        foreach ($formerDir in $formerDirs) {
            $formerExe = Join-Path $formerDir 'choregos.exe'
            if (Test-Path -LiteralPath "$formerExe.old") { Remove-Item -LiteralPath "$formerExe.old" -Force -ErrorAction SilentlyContinue }
            if (Test-Path -LiteralPath $formerExe) {
                Move-Item -LiteralPath $formerExe -Destination "$formerExe.old" -Force -ErrorAction SilentlyContinue
                Remove-Item -LiteralPath "$formerExe.old" -Force -ErrorAction SilentlyContinue
            }
        }
        Write-Host "The former command, choregos, runs it too ($(Join-Path $InstallDir 'choregos.cmd'))."

        # On the user's PATH, for the next terminals, and for this one.
        $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        if (-not $userPath) { $userPath = '' }
        $entries = $userPath.Split(';') | Where-Object { $_ -ne '' }
        if ($entries -notcontains $InstallDir) {
            [Environment]::SetEnvironmentVariable('Path', (($entries + $InstallDir) -join ';'), 'User')
            Write-Host "Added $InstallDir to your user PATH (new terminals will see it)."
        }
        if (($env:Path.Split(';')) -notcontains $InstallDir) { $env:Path = "$env:Path;$InstallDir" }

        Write-Host ''
        Write-Host 'Next: sign in with your browser'
        Write-Host "  tranibor login --api-url $InstanceUrl"
        Write-Host 'For a script or a CI job, save an access token instead (My account, Access tokens):'
        Write-Host "  tranibor login --api-url $InstanceUrl --token <token>"
        Write-Host 'Keep it in step with the instance later with: tranibor self update'
    } finally {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Install-Tranibor
