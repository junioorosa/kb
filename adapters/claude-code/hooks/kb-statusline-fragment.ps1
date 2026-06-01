# kb-statusline-fragment.ps1 -- emits "[KB <h> B:<short>* <T> <n>/<total>]"
#
# Single-shot statusline fragment. No counters per tool; reads only:
#   - kb-tier-<session>.json    : last_tier + hits/total bumped by kb_retrieve
#   - kb-session-branch-<sid>.json : branch + manual_override (sidecar)
#   - kb-embed-daemon.lock      : daemon health (port + TCP ping)
#   - kb-hooks-disabled (file) / KB_HOOKS_DISABLED env: kill switch
#
# Output examples (glyphs U+2713 / U+21BB / U+2757 / U+2717 generated at runtime
# to keep the source ASCII so PowerShell 5.1 parses it regardless of the BOM):
#   [KB OK B:feat/foo* H 4/7]   daemon up, model loaded     (check, green)
#   [KB ~  B:feat/foo* H 4/7]   daemon up, model loading    (loop,  cyan)
#   [KB W  B:feat/foo* H 4/7]   daemon down/mute -> BM25    (warn,  orange)
#   [KB X ]                     hooks disabled (kill)       (cross, gray)
#
# Health is a `ping`: green only when model_loaded is true (embedding retrieval
# ready). Reachable-but-loading is a distinct state, NOT the BM25-fallback warn,
# so a fresh daemon's background model load doesn't flash a false warning. A
# port that connects but won't answer the ping (defunct socket after sleep)
# stays warn -- the old connect-only probe wrongly showed that green.
#
# Tier glyph: H/M/L/- (dash = no retrieval yet this session).

try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch {}

$ClaudeDir = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME ".claude" }
$StateDir  = Join-Path $ClaudeDir "state"
$Esc       = [char]27

# Unicode glyphs as char codes (ASCII source -> Unicode at runtime)
$GlyphOk   = [char]0x2713
$GlyphWarn = [char]0x2757   # heavy exclamation -- heavier than U+26A0 warning triangle
$GlyphLoad = [char]0x21BB   # clockwise open-circle arrow, "model warming up"
$GlyphCross= [char]0x2717
$GlyphDash = [char]0x2014   # em dash, "tier=none"

function Color([string]$code, [string]$text) {
    return $Esc + "[38;5;" + $code + "m" + $text + $Esc + "[0m"
}

# --- Kill switch ------------------------------------------------------------
$disabledFile = Join-Path $ClaudeDir "kb-hooks-disabled"
if ((Test-Path -LiteralPath $disabledFile) -or ($env:KB_HOOKS_DISABLED -eq '1')) {
    [Console]::Out.Write((Color "244" ("[KB " + $GlyphCross + "]")))
    exit 0
}

# --- Parse session_id from stdin payload ------------------------------------
$sessionId = $null
try {
    $raw = [Console]::In.ReadToEnd()
    if (-not [string]::IsNullOrWhiteSpace($raw)) {
        $payload = $raw | ConvertFrom-Json -ErrorAction Stop
        if ($payload.session_id) { $sessionId = [string]$payload.session_id }
    }
} catch {}
if ([string]::IsNullOrEmpty($sessionId)) { exit 0 }

$safeSession = $sessionId -replace '[^a-zA-Z0-9\-_]', ''
if ($safeSession.Length -eq 0) { exit 0 }

function Read-JsonSafe {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
        $isReparse = ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)
        if ($isReparse -or $item.Length -gt 65536) { return $null }
        return Get-Content -LiteralPath $Path -Raw -Encoding utf8 -ErrorAction Stop |
               ConvertFrom-Json -ErrorAction Stop
    } catch { return $null }
}

# --- Branch (from sidecar) --------------------------------------------------
$branchSegment = ""
$sidecar = Read-JsonSafe (Join-Path $StateDir "kb-session-branch-$safeSession.json")
if ($sidecar -and $sidecar.PSObject.Properties.Name -contains 'branch') {
    $branch = [string]$sidecar.branch
    if (-not [string]::IsNullOrWhiteSpace($branch)) {
        # Shortens "feat/39458-dashboard-..." to "feat/39458"; falls back to full name.
        if ($branch -match '^([^/]+/(?:\d+|[A-Za-z]+-\d+))') {
            $short = $matches[1]
        } else {
            $short = $branch
        }
        $star = ""
        if (($sidecar.PSObject.Properties.Name -contains 'manual_override') -and $sidecar.manual_override) {
            $star = "*"
        }
        $branchSegment = " B:" + $short + $star
    }
}

# --- Tier + hit ratio (from kb-tier-<session>.json) -------------------------
$tier  = "none"
$hits  = 0
$total = 0
$tierState = Read-JsonSafe (Join-Path $StateDir "kb-tier-$safeSession.json")
if ($tierState) {
    if ($tierState.PSObject.Properties.Name -contains 'last_tier') { $tier = [string]$tierState.last_tier }
    if ($tierState.PSObject.Properties.Name -contains 'hits')      { try { $hits  = [int]$tierState.hits  } catch {} }
    if ($tierState.PSObject.Properties.Name -contains 'total')     { try { $total = [int]$tierState.total } catch {} }
}

$tierGlyph = switch ($tier) {
    "high" { "H" }
    "mid"  { "M" }
    "low"  { "L" }
    default { [string]$GlyphDash }
}

$ratioSegment = ""
if ($total -gt 0) { $ratioSegment = " " + $hits + "/" + $total }

# --- Daemon health ----------------------------------------------------------
# OK   (green) : daemon answers ping AND model_loaded == true (embeddings ready)
# LOAD (cyan)  : daemon answers ping but model still loading (no BM25 fallback)
# WARN (orange): unreachable, OR connects but won't answer the ping (defunct)
$healthGlyph = $GlyphWarn
$healthColor = "214"
$lock = Join-Path $StateDir "kb-embed-daemon.lock"
if (Test-Path -LiteralPath $lock) {
    $lockJson = Read-JsonSafe $lock
    if ($lockJson -and ($lockJson.PSObject.Properties.Name -contains 'port')) {
        $port = 0
        try { $port = [int]$lockJson.port } catch {}
        if ($port -gt 0) {
            $sock = New-Object System.Net.Sockets.TcpClient
            try {
                $async = $sock.BeginConnect('127.0.0.1', $port, $null, $null)
                $ok = $async.AsyncWaitHandle.WaitOne(200, $false)
                if ($ok -and $sock.Connected) {
                    $sock.EndConnect($async) | Out-Null
                    # Ping the daemon: a single TCP segment carries the whole
                    # reply, so one Read with a short timeout gets the full line.
                    # `ping` never needs the model loaded, so it answers instantly
                    # even mid-load -- letting us tell "loading" from "down".
                    $stream = $sock.GetStream()
                    $stream.ReadTimeout  = 300
                    $stream.WriteTimeout = 200
                    $req = [System.Text.Encoding]::UTF8.GetBytes('{"op":"ping"}' + "`n")
                    $stream.Write($req, 0, $req.Length)
                    $buf = New-Object byte[] 1024
                    $n = $stream.Read($buf, 0, $buf.Length)
                    if ($n -gt 0) {
                        $resp = [System.Text.Encoding]::UTF8.GetString($buf, 0, $n)
                        $pong = $resp | ConvertFrom-Json -ErrorAction Stop
                        $hasFlag = $pong.PSObject.Properties.Name -contains 'model_loaded'
                        if ($hasFlag -and (-not $pong.model_loaded)) {
                            $healthGlyph = $GlyphLoad   # up, warming up
                            $healthColor = "44"         # cyan
                        } else {
                            $healthGlyph = $GlyphOk     # up + ready (or old daemon w/o flag)
                            $healthColor = "82"
                        }
                    }
                    # $n == 0 or parse/timeout throw -> stays WARN (defunct socket)
                }
            } catch {} finally { $sock.Close() }
        }
    }
}

# --- Assemble ---------------------------------------------------------------
# Each segment carries its own ANSI color so the [0m reset between them does
# not bleed into the next part. Nesting (outer wrap + inner colors) would
# reset to terminal default after the inner [0m, leaving downstream text gray.
$blue = "75"
$tierColor = switch ($tier) {
    "high"  { "196" }   # red
    "mid"   { "214" }   # orange
    "low"   { "244" }   # gray
    default { "244" }   # dash (none) = gray
}

$parts = @()
$parts += (Color $blue "[KB ")
$parts += (Color $healthColor ([string]$healthGlyph))
if ($branchSegment) { $parts += (Color $blue $branchSegment) }
$parts += (Color $blue " ")
$parts += (Color $tierColor $tierGlyph)
if ($ratioSegment) { $parts += (Color $blue $ratioSegment) }
$parts += (Color $blue "]")

[Console]::Out.Write((-join $parts))
exit 0
