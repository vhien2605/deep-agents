<#
.SYNOPSIS
  Run the benchmark CLI inside the container, mounting this repo at /app.

.EXAMPLE
  bench\run-docker.ps1 discover --project data/re2-... --limit 20
  bench\run-docker.ps1 run --project data/re2-... --out runs/re2 --sample 30

  Set $env:BENCH_REBUILD = "1" to force an image rebuild.
#>
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$image = if ($env:BENCH_IMAGE) { $env:BENCH_IMAGE } else { "deepagents-bench" }

docker image inspect $image *> $null
$imageMissing = -not $?
if ($env:BENCH_REBUILD -eq "1" -or $imageMissing) {
    docker build -t $image -f (Join-Path $repoRoot "bench\Dockerfile") $repoRoot
    if ($LASTEXITCODE -ne 0) { throw "image build failed" }
}

# No --env-file: docker keeps the quotes in a value like LLM_API_KEY="sk-...",
# and load_dotenv() inside the container will not override an already-set
# variable, so a quoted key would poison every run. The repo (including .env)
# is mounted at /app, where python-dotenv parses it correctly. Host variables
# are still forwarded so CI can override without touching the file.
$runArgs = @("run", "--rm", "-i", "-v", "${repoRoot}:/app", "-w", "/app")
foreach ($name in @("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "AGENT_RECURSION_LIMIT",
                    "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL")) {
    $value = [Environment]::GetEnvironmentVariable($name)
    if ($value) { $runArgs += @("-e", "$name=$value") }
}
$runArgs += $image
$runArgs += $args

docker @runArgs
exit $LASTEXITCODE
