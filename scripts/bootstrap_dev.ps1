#Requires -Version 5.1
<#
.SYNOPSIS
    Bring a bare Windows box up to "can build this app" state. One command, any project.

.DESCRIPTION
    The homogeneous dev-prerequisite protocol inherited by every minted app.
    A fresh Windows machine has none of the Rust half of the toolchain; this
    script detects what's missing, installs it, and verifies the result.

    Two tiers:
      BUILD    (default)        -> enough to `cargo run`
                                   rustup + Rust stable (MSVC) + MSVC C++ build tools
      PACKAGE  (-IncludePackaging) -> additionally enough to produce an .msi
                                   WiX Toolset v3 + cargo-wix

    Why MSVC and not GNU: the default host triple is x86_64-pc-windows-msvc and
    CI (windows-latest + dtolnay/rust-toolchain@stable) builds MSVC. Matching CI
    locally is the whole point; a GNU toolchain would diverge from what ships.

    Everything installs via winget, which is present on modern Windows out of the
    box. Chocolatey is deliberately NOT required — the MSI builder mentions it,
    but WiX is available from winget, so there's no reason to install a second
    package manager to get one package.

    Elevation is BATCHED: the machine-wide installers (VS Build Tools, WiX) run
    in a single elevated pass = one UAC prompt for the lot. Firing one elevated
    install per package is what produces a string of prompts, and unelevated
    per-machine MSIs fail with 1925 rather than asking.

.PARAMETER Install
    Actually install what's missing. Without this the script only reports state
    (safe to run any time to see where the box stands).

.PARAMETER IncludePackaging
    Also install the .msi packaging tier (WiX Toolset v3 + cargo-wix).

.PARAMETER SkipVerify
    Skip the closing `cargo check` verification.

.EXAMPLE
    # See what's missing, change nothing:
    powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap_dev.ps1

.EXAMPLE
    # Install the build tier, then verify it compiles:
    powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap_dev.ps1 -Install

.EXAMPLE
    # Full stack including .msi packaging:
    powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap_dev.ps1 -Install -IncludePackaging
#>
[CmdletBinding()]
param(
    [switch] $Install,
    [switch] $IncludePackaging,
    [switch] $SkipVerify
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

# ---------------------------------------------------------------- detection --
function Test-Cmd { param([string] $Name) [bool](Get-Command $Name -ErrorAction SilentlyContinue) }

function Test-Rustup { Test-Cmd rustup }
function Test-Cargo  { (Test-Cmd cargo) -or (Test-Path "$env:USERPROFILE\.cargo\bin\cargo.exe") }

function Test-Msvc {
    # The MSVC C++ toolchain is what provides link.exe. vswhere is the supported
    # way to ask; its absence means no VS/Build Tools of any kind is installed.
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path $vswhere)) { return $false }
    $found = & $vswhere -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
    return [bool]$found
}

function Test-Wix {
    if ($env:WIX -and (Test-Path (Join-Path $env:WIX 'bin\candle.exe'))) { return $true }
    if (Test-Cmd candle) { return $true }
    return [bool](Get-ChildItem "C:\Program Files (x86)" -Directory -ErrorAction SilentlyContinue |
        Where-Object Name -like 'WiX Toolset*')
}

function Test-CargoWix {
    (Test-Cmd cargo-wix) -or (Test-Path "$env:USERPROFILE\.cargo\bin\cargo-wix.exe")
}

# Each requirement: name, tier, probe, and how it gets installed.
$reqs = @(
    @{ Name = "winget";           Tier = "build";   Probe = { Test-Cmd winget }; Elevated = $false; Winget = $null
       Note = "ships with modern Windows; install App Installer from the Store if absent" }
    @{ Name = "git";              Tier = "build";   Probe = { Test-Cmd git };    Elevated = $false; Winget = "Git.Git" }
    # No --scope here: Rustlang.Rustup publishes no user-scope installer, and passing
    # --scope user filters out the only applicable one ("No applicable installer found").
    # rustup-init is per-user by nature anyway (~/.rustup, ~/.cargo).
    @{ Name = "rustup + cargo";   Tier = "build";   Probe = { (Test-Rustup) -and (Test-Cargo) }; Elevated = $false; Winget = "Rustlang.Rustup" }
    @{ Name = "MSVC C++ tools";   Tier = "build";   Probe = { Test-Msvc };       Elevated = $true
       Winget = "Microsoft.VisualStudio.2022.BuildTools"
       Override = "--quiet --wait --norestart --nocache --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
       Note = "large (~2-6 GB): provides link.exe + Windows SDK that the msvc target needs" }
    @{ Name = "WiX Toolset v3";   Tier = "package"; Probe = { Test-Wix };        Elevated = $true; Winget = "WiXToolset.WiXToolset"
       Note = "provides candle/light that cargo-wix drives" }
    @{ Name = "cargo-wix";        Tier = "package"; Probe = { Test-CargoWix };   Elevated = $false; Cargo = "cargo-wix"
       Note = "front-end only; useless without the WiX Toolset above" }
)

$wanted = if ($IncludePackaging) { @("build", "package") } else { @("build") }
$active = $reqs | Where-Object { $wanted -contains $_.Tier }

function Show-State {
    Write-Host ""
    Write-Host ("{0,-20} {1,-9} {2}" -f "REQUIREMENT", "TIER", "STATE")
    Write-Host ("{0,-20} {1,-9} {2}" -f "-----------", "----", "-----")
    foreach ($r in $active) {
        $ok = [bool](& $r.Probe)
        $state = if ($ok) { "present" } else { "MISSING" }
        Write-Host ("{0,-20} {1,-9} {2}" -f $r.Name, $r.Tier, $state)
    }
    Write-Host ""
}

Write-Host "Project: $root"
Write-Host "Tiers:   $($wanted -join ' + ')"
Show-State

$missing = @($active | Where-Object { -not (& $_.Probe) })

if ($missing.Count -eq 0) {
    Write-Host "All requirements satisfied - nothing to install."
}
elseif (-not $Install) {
    Write-Host "Missing: $(($missing | ForEach-Object { $_.Name }) -join ', ')"
    foreach ($m in $missing) { if ($m.Note) { Write-Host "  - $($m.Name): $($m.Note)" } }
    Write-Host ""
    Write-Host "Re-run with -Install to install them (add -IncludePackaging for the .msi tier)."
    return
}
else {
    if (-not (Test-Cmd winget)) {
        throw "winget is required to bootstrap and is not present. Install 'App Installer' from the Microsoft Store, then re-run."
    }

    # -- unelevated winget installs --
    # Scope is opt-in per requirement, never blanket: not every package publishes a
    # user-scope installer, and forcing --scope on one that doesn't makes winget
    # report "No applicable installer found" instead of installing the one it has.
    foreach ($m in $missing | Where-Object { $_.Winget -and -not $_.Elevated }) {
        Write-Host "Installing $($m.Name) [$($m.Winget)] ..."
        $args = @("install", "--id", $m.Winget, "-e", "--source", "winget",
                  "--accept-package-agreements", "--accept-source-agreements")
        if ($m.Scope) { $args += @("--scope", $m.Scope) }
        winget @args
    }

    # -- elevated winget installs, BATCHED into a single UAC prompt --
    $elev = @($missing | Where-Object { $_.Winget -and $_.Elevated })
    if ($elev.Count -gt 0) {
        Write-Host ""
        Write-Host "The following need administrator rights and will be installed in ONE elevated batch:"
        foreach ($e in $elev) { Write-Host "  - $($e.Name) [$($e.Winget)]" }
        Write-Host "Approve the single UAC prompt when it appears."

        $lines = foreach ($e in $elev) {
            $cmd = "winget install --id $($e.Winget) -e --source winget --accept-package-agreements --accept-source-agreements"
            if ($e.Override) { $cmd += " --override `"$($e.Override)`"" }
            $cmd
        }
        $batch = Join-Path $env:TEMP "bootstrap_dev_elevated.ps1"
        $lines | Set-Content -LiteralPath $batch -Encoding UTF8

        $p = Start-Process -FilePath "powershell.exe" `
            -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $batch) `
            -Verb RunAs -Wait -PassThru
        Remove-Item -LiteralPath $batch -Force -ErrorAction SilentlyContinue
        if ($p.ExitCode -ne 0) {
            Write-Warning "Elevated batch exited $($p.ExitCode) - some machine-wide installs may have failed."
        }
    }

    # PATH from installers won't be in THIS process; pull it in so later steps see it.
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
    if (Test-Path "$env:USERPROFILE\.cargo\bin") { $env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path" }

    # -- cargo-installed tools (need cargo on PATH first) --
    foreach ($m in $missing | Where-Object { $_.Cargo }) {
        if (-not (Test-Cargo)) { Write-Warning "cargo unavailable - skipping $($m.Name). Re-run in a new shell."; continue }
        Write-Host "Installing $($m.Name) via cargo ..."
        cargo install $m.Cargo --locked
    }

    # rustup may install without a default toolchain; make sure stable-msvc is in.
    if (Test-Rustup) {
        Write-Host "Ensuring stable-x86_64-pc-windows-msvc toolchain ..."
        rustup toolchain install stable-x86_64-pc-windows-msvc
        rustup default stable-x86_64-pc-windows-msvc
    }

    Write-Host ""
    Write-Host "Post-install state:"
    Show-State
}

# ------------------------------------------------------------- verification --
if ($SkipVerify) { Write-Host "Verification skipped (-SkipVerify)."; return }

$stillMissing = @($active | Where-Object { -not (& $_.Probe) })
if ($stillMissing.Count -gt 0) {
    Write-Warning "Blocked - still missing: $(($stillMissing | ForEach-Object { $_.Name }) -join ', ')"
    Write-Host "Installers modify PATH; a NEW shell often resolves this. Re-run to re-check."
    exit 1
}

Write-Host "Verifying the project actually compiles (cargo check) ..."
Push-Location $root
try {
    cargo check
    $code = $LASTEXITCODE
}
finally { Pop-Location }

if ($code -eq 0) { Write-Host ""; Write-Host "READY - toolchain satisfied and the project compiles." }
else { Write-Warning "Toolchain present but 'cargo check' failed ($code) - see output above."; exit 1 }
