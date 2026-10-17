$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$configDir = Join-Path $repoRoot 'var/knowledge'
$configFile = Join-Path $configDir 'local.env'
if (Test-Path -LiteralPath $configFile) {
    Write-Output 'Existing knowledge configuration preserved.'
    exit 0
}
New-Item -ItemType Directory -Path $configDir -Force | Out-Null
function New-LocalPassword {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return ([BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
}
$adminPassword = New-LocalPassword
$appPassword = New-LocalPassword
$settings = @(
    "KNOWLEDGE_DB_PASSWORD=$adminPassword"
    "KNOWLEDGE_APP_PASSWORD=$appPassword"
    'KNOWLEDGE_DB_PORT=55432'
    "FLOWDELTA_KB_MIGRATION_URL=postgresql+psycopg://flowdelta_admin:${adminPassword}@127.0.0.1:55432/flowdelta_knowledge"
    "FLOWDELTA_KB_DATABASE_URL=postgresql+psycopg://flowdelta_app:${appPassword}@127.0.0.1:55432/flowdelta_knowledge"
    'FLOWDELTA_KB_RAW_ROOT=var/knowledge/raw'
)
[System.IO.File]::WriteAllLines($configFile, $settings, (New-Object System.Text.UTF8Encoding($false)))
Write-Output 'Created var/knowledge/local.env (credentials are not printed).'
