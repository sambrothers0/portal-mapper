<#
Repeatedly attempts to launch an OCI Always Free AMD micro (VM.Standard.E2.1.Micro)
in us-ashburn-1. Rotates through all 3 ADs each sweep. Stops on success, auth
expiry, or resource-limit hit. Sends a Windows balloon notification on success.

This is the "easy" instance: 1/8 OCPU, 1 GB RAM, x86_64, up to 2 per tenancy.
It's almost always in stock, so this usually succeeds within a few tries. The
point is to land a tiny always-on box that can then run the A1 grabber 24/7
(see grab-a1.ps1 / its Linux port) without leaving your home PC running.

Sibling of grab-a1.ps1 — same auth, backoff, notify and logging. Differences:
  * shape VM.Standard.E2.1.Micro (fixed) -> NO --shape-config
  * x86_64 image (resolved at startup) instead of the A1 aarch64 image
  * separate display name + log file so it never collides with the A1 grab

Usage: powershell -ExecutionPolicy Bypass -File grab-micro.ps1
Log:   $env:USERPROFILE\oci-micro.log

AUTH MODE - flip $AUTH_MODE once you've registered the API key in OCI Console:
  "session"  - expires ~1 hour (prompts to re-auth on expiry)
  "api_key"  - permanent; never expires; requires key registered in OCI Console
#>

$AUTH_MODE   = "api_key"   # "api_key" (permanent) or "session" (~1h)

# Keep the system awake for the duration of this script (won't prevent manual sleep via Start menu)
$_wakeType = Add-Type -MemberDefinition '[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint f);' -Name SES -Namespace WinAPI -PassThru
$_wakeType::SetThreadExecutionState([Convert]::ToUInt32('80000003', 16))  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
try { } finally { }  # defer reset to script exit below

$OCI         = "C:\Users\samjb\ocienv\Scripts\oci.exe"
$COMPARTMENT = "ocid1.tenancy.oc1..aaaaaaaana5ot43k5qutlhxxwybavgy45snzgmisruhn4h7hatzlfret6zwq"
$SUBNET      = "ocid1.subnet.oc1.iad.aaaaaaaa2zgjdq6z5zp5vc7hompzpcafdxxs5sduxdrzkqfzcsdwdst6775a"
$SSH_PUB     = "$env:USERPROFILE\.ssh\portal-mapper-oracle.pub"
$ADS         = "fivK:US-ASHBURN-AD-1", "fivK:US-ASHBURN-AD-2", "fivK:US-ASHBURN-AD-3"
$SHAPE       = "VM.Standard.E2.1.Micro"
$AD_PAUSE    = 2     # seconds between individual AD launch calls
$WAIT_SEC    = 3     # seconds between full AD sweeps
# Same throttle-backoff safety net as grab-a1.ps1: on 429/TooManyRequests we slow
# down (30 -> 60 -> 120, cap 300s) and reset once the API clears, so a burst of
# throttling can't escalate into sustained API abuse.
$THROTTLE_BASE = 30   # first backoff after a throttle, doubles each consecutive hit
$THROTTLE_CAP  = 300  # max backoff
$throttleSleep = $THROTTLE_BASE
$LOG         = "$env:USERPROFILE\oci-micro.log"

# Auth args are constant for the run; compute once and reuse (incl. the image lookup).
$authArgs = if ($AUTH_MODE -eq "api_key") {
    @("--auth", "api_key", "--profile", "API_KEY")
} else {
    @("--auth", "security_token")
}

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    Add-Content -Path $LOG -Value $line
    Write-Host $line
}

function Notify($title, $body) {
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $icon = New-Object System.Windows.Forms.NotifyIcon
        $icon.Icon    = [System.Drawing.SystemIcons]::Information
        $icon.Visible = $true
        $icon.ShowBalloonTip(20000, $title, $body, [System.Windows.Forms.ToolTipIcon]::Info)
        Start-Sleep -Seconds 3
        $icon.Dispose()
    } catch {}
    [System.Console]::Beep(880, 600)
}

# Resolve the latest x86_64 Oracle Linux 9 image compatible with the micro shape.
# Done at startup (not hardcoded) so the OCID can never go stale, and so a bad
# image lookup fails loud before we start hammering the launch API.
Log "Resolving latest x86_64 Oracle Linux image for $SHAPE..."
$IMAGE = (& $OCI compute image list `
    --compartment-id          $COMPARTMENT `
    --operating-system        "Oracle Linux" `
    --operating-system-version "9" `
    --shape                   $SHAPE `
    --sort-by                 TIMECREATED `
    --sort-order              DESC `
    --limit                   1 `
    @authArgs `
    --query                   'data[0].id' `
    --raw-output 2>&1 | Out-String).Trim()

if ($IMAGE -notmatch '^ocid1\.image\.') {
    Log "ERROR - could not resolve an image OCID. Got: $IMAGE"
    Log "Check auth ($AUTH_MODE) and that an Oracle Linux 9 x86_64 image exists in this region."
    Notify "OCI micro grab failed" "Could not resolve a base image - see oci-micro.log."
    exit 4
}
Log "Using image $IMAGE"

Log "=== Micro grab started - $SHAPE x86_64, ~${AD_PAUSE}s/AD + ${WAIT_SEC}s/sweep, auto-backoff on throttle ==="

$n = 0       # full-sweep counter
$tries = 0   # individual launch-call counter
while ($true) {
    $n++
    foreach ($ad in $ADS) {
        $tries++
        Log "Sweep $n / try $tries - $ad"

        # NOTE: E2.1.Micro is a FIXED shape -> no --shape-config (passing one errors).
        $out = & $OCI compute instance launch `
            --compartment-id       $COMPARTMENT `
            --availability-domain  $ad `
            --shape                $SHAPE `
            --image-id             $IMAGE `
            --subnet-id            $SUBNET `
            --ssh-authorized-keys-file $SSH_PUB `
            --display-name         "portal-mapper-micro" `
            --assign-public-ip     true `
            @authArgs `
            --output               json 2>&1 | Out-String

        $code = $LASTEXITCODE

        if ($code -eq 0 -and $out -match '"id"\s*:\s*"ocid1\.instance') {
            Log "SUCCESS in $ad after $tries tries ($n sweeps)"
            Log $out
            Notify "OCI micro is UP!" "Instance launched in $ad - open console."
            exit 0

        } elseif ($out -match 'NotAuthenticated|token.*expired|security token|401') {
            Log "AUTH EXPIRED"
            Log "Re-authenticate: C:\Users\samjb\ocienv\Scripts\oci.exe session authenticate --region us-ashburn-1 --profile-name DEFAULT"
            Notify "OCI auth expired" "Re-run: ocienv\Scripts\oci.exe session authenticate --region us-ashburn-1"
            exit 2

        } elseif ($out -match 'LimitExceeded') {
            Log "RESOURCE LIMIT EXCEEDED - you may already have 2 micro instances (the Always Free max)"
            Notify "OCI limit exceeded" "Check your console - micro limit hit (max 2)."
            exit 3

        } elseif ($out -match 'TooManyRequests|429|throttl') {
            # OCI is rate-limiting us - back off (exponential, capped) and reset
            # the cadence so we don't keep poking an angry API.
            Log "  THROTTLED (429) - backing off ${throttleSleep}s"
            Start-Sleep -Seconds $throttleSleep
            $throttleSleep = [Math]::Min($throttleSleep * 2, $THROTTLE_CAP)
            continue   # skip the normal inter-AD pause; backoff already covered it

        } else {
            # InternalError / out-of-capacity - expected, continue rotating
            $throttleSleep = $THROTTLE_BASE   # clean response: clear any backoff
            $snippet = ($out -replace '\s+', ' ').Trim()
            if ($snippet.Length -gt 120) { $snippet = $snippet.Substring(0, 120) + "..." }
            Log "  No capacity (exit $code): $snippet"
        }

        Start-Sleep -Seconds $AD_PAUSE   # brief pause between ADs
    }

    Log "All ADs full - sleeping ${WAIT_SEC}s..."
    Start-Sleep -Seconds $WAIT_SEC
}

$_wakeType::SetThreadExecutionState([Convert]::ToUInt32('80000000', 16))  # ES_CONTINUOUS — restore normal sleep
