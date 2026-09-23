# Everything that launches itself at sign-in, in one list.
#
# Windows starts things from five different places and no single UI shows them
# all, which is how duplicates survive for years. This gathers:
#   - Startup folder (this user, and all users)
#   - Run / RunOnce registry keys (HKCU and HKLM, 64- and 32-bit)
#   - Scheduled tasks that trigger at logon
#
# Read-only. It reports and flags duplicates; it removes nothing.
#
#   powershell -ExecutionPolicy Bypass -File startup-audit.ps1

$ErrorActionPreference = 'SilentlyContinue'
$e = [char]27
$dim = "$e[2m"; $bold = "$e[1m"; $red = "$e[31m"; $yellow = "$e[33m"; $r = "$e[0m"

$items = New-Object System.Collections.Generic.List[object]

function Add-Item2($source, $name, $target) {
    if (-not $name) { return }
    $script:items.Add([pscustomobject]@{
        Source = $source; Name = $name; Target = ($target -replace '^"|"$', '')
    })
}

# ---- Startup folders -------------------------------------------------------
$folders = @(
    @{ p = [Environment]::GetFolderPath('Startup');       s = 'Startup (user)' },
    @{ p = [Environment]::GetFolderPath('CommonStartup'); s = 'Startup (all users)' }
)
foreach ($f in $folders) {
    if (-not (Test-Path $f.p)) { continue }
    Get-ChildItem $f.p -File | Where-Object { $_.Name -ne 'desktop.ini' } | ForEach-Object {
        $target = $_.FullName
        if ($_.Extension -eq '.lnk') {
            try {
                $sh = New-Object -ComObject WScript.Shell
                $target = $sh.CreateShortcut($_.FullName).TargetPath
            } catch { }
        }
        Add-Item2 $f.s $_.BaseName $target
    }
}

# ---- Run keys --------------------------------------------------------------
$keys = @(
    @{ k = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run';                        s = 'Run (user)' },
    @{ k = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run';                        s = 'Run (machine)' },
    @{ k = 'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run';            s = 'Run (machine 32-bit)' },
    @{ k = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce';                    s = 'RunOnce (user)' }
)
foreach ($k in $keys) {
    if (-not (Test-Path $k.k)) { continue }
    $props = Get-ItemProperty $k.k
    $props.PSObject.Properties |
        Where-Object { $_.Name -notlike 'PS*' } |
        ForEach-Object { Add-Item2 $k.s $_.Name ([string]$_.Value) }
}

# ---- Logon scheduled tasks -------------------------------------------------
Get-ScheduledTask | Where-Object {
    $_.State -ne 'Disabled' -and ($_.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' })
} | ForEach-Object {
    $act = ($_.Actions | Select-Object -First 1).Execute
    Add-Item2 'Task (logon)' $_.TaskName $act
}

# ---- Report ----------------------------------------------------------------
"`n ${bold}STARTUP ITEMS${r}  ${dim}$($items.Count) total${r}`n"

$items | Sort-Object Source, Name | ForEach-Object {
    $t = $_.Target
    if ($t.Length -gt 52) { $t = '...' + $t.Substring($t.Length - 49) }
    "{0,-22} {1,-26} {2}{3}{4}" -f $_.Source, $_.Name, $dim, $t, $r
}

# Duplicates: same executable launched from more than one place.
# Registry Run values carry arguments ('"...\Discord.exe" --start-inactive'),
# so the exe has to be parsed out first - taking GetFileName on the whole
# string returns empty and lumps every argument-bearing entry together.
function Get-Exe($cmd) {
    if (-not $cmd) { return '' }
    $c = $cmd.Trim()
    if ($c.StartsWith('"')) {
        $end = $c.IndexOf('"', 1)
        if ($end -gt 1) { $c = $c.Substring(1, $end - 1) }
    } else {
        $m = [regex]::Match($c, '^(.*?\.exe)\b')
        if ($m.Success) { $c = $m.Groups[1].Value }
    }
    try { return [IO.Path]::GetFileName($c).ToLower() } catch { return '' }
}

$dupes = $items | Where-Object { $_.Target } |
         Group-Object { Get-Exe $_.Target } |
         Where-Object { $_.Count -gt 1 -and $_.Name } |
         Where-Object { $_.Name -like '*.exe' }

if ($dupes) {
    "`n ${red}${bold}DUPLICATES${r}  ${dim}the same program started more than once${r}`n"
    foreach ($d in $dupes) {
        " ${yellow}$($d.Name)${r} - $($d.Count) entries:"
        $d.Group | ForEach-Object { "     $($_.Source.PadRight(22)) $($_.Name)" }
    }
} else {
    "`n ${dim}no duplicates found${r}"
}

"`n ${dim}This script changes nothing. To remove a Startup-folder item, delete its"
"shortcut. To remove a Run key, delete that value. To stop a task, Disable it.${r}`n"
