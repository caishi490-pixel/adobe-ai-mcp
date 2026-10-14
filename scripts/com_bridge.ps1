# adobe-ai-mcp · COM bridge for Adobe Illustrator
# Reads a JSX file, runs it inside Illustrator via COM (late binding),
# writes a JSON envelope to the output file:
#   { ok: true,  wasRunning: bool, retries: int, result: "<DoJavaScript return>" }
#   { ok: false, code: "not_running"|"not_installed"|"launch_timeout"|"com_error"|"error",
#     wasRunning: bool, retries: int, error: "..." }
# ASCII-only on purpose: PowerShell 5.1 reads BOM-less .ps1 as ANSI.
param(
  [Parameter(Mandatory=$true)][string]$JsxPath,
  [Parameter(Mandatory=$true)][string]$OutPath,
  [switch]$Launch
)
$ErrorActionPreference = 'Stop'

function Write-Result($obj) {
  $json = $obj | ConvertTo-Json -Depth 8 -Compress
  [System.IO.File]::WriteAllText($OutPath, $json, (New-Object System.Text.UTF8Encoding($false)))
}

$wasRunning = $false
$retries = 0

try {
  $progId = 'Illustrator.Application'
  $type = [System.Type]::GetTypeFromProgID($progId)
  if ($null -eq $type) {
    Write-Result @{ ok = $false; code = 'not_installed'; error = 'Adobe Illustrator is not installed (ProgID not found).' }
    exit 0
  }

  $app = $null
  try {
    $app = [System.Runtime.InteropServices.Marshal]::GetActiveObject($progId)
    $wasRunning = $true
  } catch {
    if (-not $Launch) {
      Write-Result @{ ok = $false; code = 'not_running'; wasRunning = $false; error = 'Illustrator is not running.' }
      exit 0
    }
    $app = [System.Activator]::CreateInstance($type)
    $deadline = (Get-Date).AddSeconds(120)
    $ready = $false
    while (-not $ready -and (Get-Date) -lt $deadline) {
      try { $null = $app.Documents.Count; $ready = $true } catch { Start-Sleep -Milliseconds 2000 }
    }
    if (-not $ready) {
      Write-Result @{ ok = $false; code = 'launch_timeout'; wasRunning = $false; error = 'Illustrator did not become ready within 120 seconds.' }
      exit 0
    }
    Start-Sleep -Seconds 2
  }

  $jsx = [System.IO.File]::ReadAllText($JsxPath, [System.Text.Encoding]::UTF8)

  $maxAttempts = 1
  if (-not $wasRunning) { $maxAttempts = 3 }
  for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
    try {
      $r = $app.DoJavaScript($jsx)
      Write-Result @{ ok = $true; wasRunning = $wasRunning; retries = ($attempt - 1); result = [string]$r }
      exit 0
    } catch {
      if ($attempt -ge $maxAttempts) { throw }
      $retries = $attempt
      Start-Sleep -Seconds 4
    }
  }
} catch {
  $msg = $_.Exception.Message
  $code = 'error'
  if ($_.Exception -is [System.Runtime.InteropServices.COMException]) { $code = 'com_error' }
  try { Write-Result @{ ok = $false; code = $code; wasRunning = $wasRunning; retries = $retries; error = $msg } } catch { }
  exit 0
}
