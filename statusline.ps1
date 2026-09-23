# Claude Code status line - shows live context/token usage.
# Receives session JSON on stdin. Must never throw: a crash here blanks the bar.
#
# NOTE: every non-ASCII glyph is built from [char] codes on purpose. Windows
# PowerShell 5.1 reads .ps1 as ANSI unless the file has a UTF-8 BOM, which
# silently corrupts literal box-drawing characters and breaks the script.

$ErrorActionPreference = 'SilentlyContinue'

$raw = $input | Out-String
if (-not $raw.Trim()) { exit 0 }
try { $j = $raw | ConvertFrom-Json } catch { exit 0 }

$e      = [char]27
$dim    = "$e[38;5;245m"
$text   = "$e[38;5;252m"
$accent = "$e[38;5;180m"
$green  = "$e[38;5;114m"
$amber  = "$e[38;5;179m"
$red    = "$e[38;5;174m"
$reset  = "$e[0m"

$BLOCK_FULL  = [char]0x2588   # full block
$BLOCK_LIGHT = [char]0x2591   # light shade
$MIDDOT      = [char]0x00B7   # middle dot

$sep = "$dim  $MIDDOT  $reset"

function Format-Tokens($n) {
    if ($null -eq $n) { return "?" }
    if ($n -ge 1000000) { return ("{0:N1}M" -f ($n / 1000000)) }
    if ($n -ge 1000)    { return ("{0:N1}k" -f ($n / 1000)) }
    return [string][int]$n
}

# --- directory -------------------------------------------------------------
$cwd = $j.workspace.current_dir
if (-not $cwd) { $cwd = $j.cwd }
$dir = if ($cwd) { Split-Path $cwd -Leaf } else { "" }

# --- model -----------------------------------------------------------------
$model = $j.model.display_name
if (-not $model) { $model = $j.model.id }

# Local models route through Ollama; flag that instead of a meaningless cost.
$isLocal = $env:ANTHROPIC_BASE_URL -and ($env:ANTHROPIC_BASE_URL -match 'localhost|127\.0\.0\.1')

$parts = @()
if ($dir)   { $parts += "$text$dir$reset" }
if ($model) { $parts += "$accent$model$reset" }

# --- context window --------------------------------------------------------
$cw = $j.context_window
if ($cw) {
    $inTok  = [int]$cw.total_input_tokens
    $outTok = [int]$cw.total_output_tokens
    $used   = $inTok + $outTok
    $size   = [int]$cw.context_window_size

    # CLAUDE_CODE_MAX_CONTEXT_TOKENS is the real window for a local model;
    # prefer it over the 200k Claude Code assumes for unrecognized models.
    if ($env:CLAUDE_CODE_MAX_CONTEXT_TOKENS) {
        $override = [int]$env:CLAUDE_CODE_MAX_CONTEXT_TOKENS
        if ($override -gt 0) { $size = $override }
    }

    if ($size -gt 0 -and $used -gt 0) {
        $pct = [math]::Round(($used / $size) * 100)
        if ($pct -gt 100) { $pct = 100 }

        $color = if ($pct -ge 80) { $red } elseif ($pct -ge 55) { $amber } else { $green }

        $width  = 14
        $filled = [int][math]::Floor(($pct / 100) * $width)
        if ($filled -gt $width) { $filled = $width }
        if ($filled -lt 0) { $filled = 0 }

        $barOn  = [string]$BLOCK_FULL  * $filled
        $barOff = [string]$BLOCK_LIGHT * ($width - $filled)
        $bar    = $color + $barOn + $dim + $barOff + $reset

        $parts += "$bar $color$pct%$reset"
        $parts += ($text + (Format-Tokens $used) + $dim + "/" + (Format-Tokens $size) + $reset)
        $parts += ($dim + "out " + $reset + $text + (Format-Tokens $outTok) + $reset)
    }
}

# --- cost / origin ---------------------------------------------------------
if ($isLocal) {
    $parts += ($green + "local" + $reset)
} elseif ($null -ne $j.cost.total_cost_usd -and $j.cost.total_cost_usd -gt 0) {
    $parts += ($dim + '$' + ("{0:N2}" -f $j.cost.total_cost_usd) + $reset)
}

Write-Host ($parts -join $sep)
