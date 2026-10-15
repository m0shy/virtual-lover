# Scan git-tracked files for accidentally committed secrets/tokens.
# Usage: scripts\check-secrets.bat  (wrapper prints Chinese context)
$ErrorActionPreference = 'Continue'
Set-Location (Join-Path $PSScriptRoot '..')

# Built-in generic patterns (contain no real secrets)
$patterns = @(
    'sk-[A-Za-z0-9]{20,}',
    'ark-[A-Za-z0-9-]{20,}',
    'ghp_[A-Za-z0-9]{20,}',
    'Bearer\s+[A-Za-z0-9._-]{20,}',
    'eyJ[A-Za-z0-9_-]{20,}\.'
)

# Personal patterns: one regex per line in local/secret-patterns.txt (gitignored, stays on your machine)
$personal = 'local/secret-patterns.txt'
if (Test-Path $personal) {
    # -Encoding UTF8: PS 5.1 reads BOM-less files as ANSI otherwise,
    # silently corrupting non-ASCII patterns (e.g. Chinese persona names)
    $extra = Get-Content $personal -Encoding UTF8 | Where-Object { $_ -and -not $_.TrimStart().StartsWith('#') }
    $patterns += $extra
    Write-Host ("[i] Loaded {0} personal pattern(s)" -f @($extra).Count)
}

$files = git ls-files
if (-not $files) { Write-Host '[!] Not a git repo or no tracked files'; exit 0 }

$hits = 0
foreach ($p in $patterns) {
    # --cached: scan the index (what will actually be committed), not the worktree
    git grep --cached -I -n -E $p -- . 2>$null | ForEach-Object {
        Write-Host ("[HIT] {0}  pattern {1}" -f $_, $p)
        $hits++
    }
}

# GBK-encoded .bat/.cmd files: Chinese patterns cannot match their GBK bytes,
# so decode staged bat files as codepage 936 and match every pattern again
function Convert-GitPath([string]$s) {
    # git quotes non-ASCII paths as "\344\270\255..."; decode octal escapes back to UTF-8
    if ($s.Length -lt 2 -or -not $s.StartsWith('"')) { return $s }
    $inner = $s.Substring(1, $s.Length - 2)
    $bytes = New-Object System.Collections.Generic.List[byte]
    $i = 0
    while ($i -lt $inner.Length) {
        if ($inner[$i] -eq '\' -and $i + 3 -lt $inner.Length) {
            $oct = $inner.Substring($i + 1, 3)
            try { $bytes.Add([Convert]::ToByte($oct, 8)); $i += 4; continue } catch { }
        }
        $bytes.Add([byte][char]$inner[$i]); $i++
    }
    [Text.Encoding]::UTF8.GetString($bytes.ToArray())
}
$stagedBats = git diff --cached --name-only --diff-filter=ACM -- '*.bat' '*.cmd' 2>$null
foreach ($raw in $stagedBats) {
    $f = Convert-GitPath $raw.Trim()
    if (-not (Test-Path -LiteralPath $f)) { Write-Host ("[WARN] staged bat not found: {0}" -f $f); continue }
    try {
        $bytes = [IO.File]::ReadAllBytes($f)
        $text = [Text.Encoding]::GetEncoding(936).GetString($bytes)
        $lines = $text -split "`n"
        for ($i = 0; $i -lt $lines.Count; $i++) {
            foreach ($p in $patterns) {
                if ($lines[$i] -match $p) {
                    Write-Host ("[HIT] {0}:{1} (GBK)  pattern {2}" -f $f, ($i + 1), $p)
                    $hits++
                }
            }
        }
    } catch {
        Write-Host ("[WARN] cannot decode as GBK, skipped: {0}" -f $f)
    }
}
if ($hits -eq 0) {
    Write-Host '[PASS] No secrets/tokens found'
    exit 0
} else {
    Write-Host ("[FAIL] {0} suspicious hit(s) - fix before committing" -f $hits)
    exit 1
}
