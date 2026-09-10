param(
    [string[]]$Urls = @('http://127.0.0.1:8765/'),
    [string]$OutputDirectory = '.operation-logs/display-chrome',
    [string]$ChromePath = "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"
)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$targetDirectory = [IO.Path]::GetFullPath((Join-Path $projectRoot $OutputDirectory))
if (-not $targetDirectory.StartsWith($projectRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'OutputDirectory must stay inside the project.'
}
if (-not (Test-Path -LiteralPath $ChromePath -PathType Leaf)) { throw 'Chrome executable was not found. Specify -ChromePath.' }
foreach ($address in $Urls) {
    $parsed = [Uri]$address
    if ($parsed.Scheme -ne 'http' -or $parsed.Host -notin @('localhost','127.0.0.1','[::1]') -or $address -match '["\s]') {
        throw 'Use a local dashboard HTTP URL without whitespace or quotes.'
    }
}
New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
$runDirectory = Join-Path $env:TEMP ('autoarticle-chrome-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $runDirectory | Out-Null
$results = @()
for ($index = 0; $index -lt $Urls.Count; $index++) {
    $stem = 'page-' + ($index + 1)
    $profile = Join-Path $runDirectory ($stem + '-profile')
    $png = Join-Path $runDirectory ($stem + '.png')
    $html = Join-Path $runDirectory ($stem + '.html')
    $stderr = Join-Path $runDirectory ($stem + '.stderr.txt')
    $arguments = @('--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
        '--window-size=1440,1100','--virtual-time-budget=5000',
        ('--user-data-dir="' + $profile + '"'), ('--screenshot="' + $png + '"'), '--dump-dom', ('"' + $Urls[$index] + '"'))
    $process = Start-Process -FilePath $ChromePath -ArgumentList $arguments -WindowStyle Hidden -PassThru -RedirectStandardOutput $html -RedirectStandardError $stderr
    if (-not $process.WaitForExit(60000)) {
        Stop-Process -Id $process.Id -ErrorAction SilentlyContinue
        throw "Chrome capture timed out. Logs: $runDirectory"
    }
    $process.Refresh()
    if (-not (Test-Path -LiteralPath $png) -or -not (Test-Path -LiteralPath $html) -or (Get-Item -LiteralPath $html).Length -eq 0) {
        throw "Chrome did not produce both artifacts. Logs: $runDirectory"
    }
    Copy-Item -LiteralPath $png -Destination (Join-Path $targetDirectory ($stem + '.png'))
    Copy-Item -LiteralPath $html -Destination (Join-Path $targetDirectory ($stem + '.html'))
    $results += @{url=$Urls[$index]; screenshot=(Join-Path $targetDirectory ($stem + '.png')); dom=(Join-Path $targetDirectory ($stem + '.html'))}
}
@{verification='render_artifacts_only; inspect_DOM_and_screenshot_before_page_checkpoint'; pages=$results; temporaryLogs=$runDirectory} | ConvertTo-Json -Depth 5
