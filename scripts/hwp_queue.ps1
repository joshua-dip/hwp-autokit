# hwp_queue.ps1 - PowerShell side of the hwp-autokit build queue (interoperates with
# hwp_autokit.lock.BuildQueue: same folder, same ticket names, same JSON records).
#
# Why: one Windows session can run only one reliable Hangul automation job at a time.
# Run the non-Hangul steps of many jobs in parallel; queue only the Hangul step.
#
# Usage (dot-source, then wrap the Hangul step):
#   . .\scripts\hwp_queue.ps1
#   $t = Enter-HwpQueue -QueueDir C:\build\queue -Tag job42 [-Priority 5]
#   try { ...Hangul work... } finally { Exit-HwpQueue $t }
#
# A waiter starts only when (1) nothing live is .running, (2) it is first in line
# (lower priority number first, then first come), and (3) no Hwp.exe is running at all,
# because someone outside the queue may be using Hangul. We wait; we never kill it.
# Owners are identified by PID *and* process start time (FILETIME UTC), so a recycled
# PID is never taken for a live owner and a crashed wrapper's entry is cleaned up.

function Get-HwpStartToken {
    param([int]$ProcessId)
    $p = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $p) { return $null }
    try { return [string]$p.StartTime.ToFileTimeUtc() } catch { return '' }
}

function Test-HwpTicketAlive {
    param([string]$File)
    try {
        $r = Get-Content -LiteralPath $File -Raw -Encoding UTF8 | ConvertFrom-Json
        $tok = Get-HwpStartToken ([int]$r.pid)
        if ($null -eq $tok) { return $false }
        $want = [string]$r.start
        return (-not $want) -or (-not $tok) -or ($tok -eq $want)
    } catch {
        # unreadable: treat a just-written file as alive, anything else as dead
        try {
            $age = (Get-Date) - (Get-Item -LiteralPath $File -ErrorAction Stop).LastWriteTime
            return ($age.TotalSeconds -lt 15)
        } catch { return $false }
    }
}

function Write-HwpQueueLog {
    param([string]$LogFile, [string]$Message)
    if ($LogFile) {
        Add-Content -Path $LogFile -Value ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $Message) -Encoding UTF8
    }
}

function Enter-HwpQueue {
    param(
        [Parameter(Mandatory)][string]$QueueDir,
        [Parameter(Mandatory)][string]$Tag,
        [string]$Label = '',
        [int]$Priority = 5,
        [int]$MaxWaitMin = 360,
        [int]$HwpWaitSec = 1800,
        [string]$ForeignName = 'Hwp',
        [string]$LogFile = ''
    )
    New-Item -ItemType Directory -Force $QueueDir | Out-Null
    $pr = [Math]::Min(9, [Math]::Max(0, $Priority))
    $safeTag = ($Tag -replace '[^\w\-\.]', '_')
    $name = 'P{0}_{1:D19}_{2}' -f $pr, [DateTime]::UtcNow.Ticks, $safeTag
    $ticket = Join-Path $QueueDir ($name + '.ticket')
    $running = Join-Path $QueueDir ($name + '.running')
    $rec = [ordered]@{
        pid = $PID; start = (Get-HwpStartToken $PID); label = $Label
        created = (Get-Date).ToString('s'); tag = $Tag; priority = $pr
    }
    $tmp = $ticket + '.' + $PID + '.tmp'
    ($rec | ConvertTo-Json -Compress) | Set-Content -LiteralPath $tmp -Encoding UTF8
    Move-Item -LiteralPath $tmp -Destination $ticket   # atomic rename: nobody sees a half-written ticket
    Write-HwpQueueLog $LogFile ("queued {0} (priority {1})" -f $Tag, $pr)

    $t0 = Get-Date
    $hwpSince = $null
    while ($true) {
        foreach ($f in @(Get-ChildItem -LiteralPath $QueueDir -File | Where-Object { $_.Extension -in '.ticket', '.running' })) {
            if (($f.FullName -ne $ticket) -and -not (Test-HwpTicketAlive $f.FullName)) {
                Remove-Item -LiteralPath $f.FullName -Force -ErrorAction SilentlyContinue
                Write-HwpQueueLog $LogFile ("removed dead entry " + $f.Name)
            }
        }
        $busy = @(Get-ChildItem -LiteralPath $QueueDir -File | Where-Object { $_.Extension -eq '.running' })
        $waiting = @(Get-ChildItem -LiteralPath $QueueDir -File | Where-Object { $_.Extension -eq '.ticket' } | Sort-Object Name)
        $myPos = 0
        for ($i = 0; $i -lt $waiting.Count; $i++) { if ($waiting[$i].FullName -eq $ticket) { $myPos = $i + 1 } }
        if ($myPos -eq 0) { throw "queue ticket vanished: $name" }

        if ($busy.Count -eq 0 -and $myPos -eq 1) {
            $foreign = @(Get-Process -Name $ForeignName -ErrorAction SilentlyContinue)
            if ($foreign.Count -eq 0) {
                Move-Item -LiteralPath $ticket -Destination $running
                Write-HwpQueueLog $LogFile ("turn started after {0:N0}s" -f ((Get-Date) - $t0).TotalSeconds)
                return $running
            }
            if (-not $hwpSince) { $hwpSince = Get-Date }
            if (((Get-Date) - $hwpSince).TotalSeconds -gt $HwpWaitSec) {
                Remove-Item -LiteralPath $ticket -Force -ErrorAction SilentlyContinue
                throw ("first in line, but {0} {1} process(es) outside the queue stayed open for {2}s" -f $foreign.Count, $ForeignName, $HwpWaitSec)
            }
        } else {
            $hwpSince = $null
        }
        if (((Get-Date) - $t0).TotalMinutes -gt $MaxWaitMin) {
            Remove-Item -LiteralPath $ticket -Force -ErrorAction SilentlyContinue
            throw ("queued for more than {0} minutes" -f $MaxWaitMin)
        }
        Start-Sleep -Seconds 3
    }
}

function Exit-HwpQueue {
    param([string]$Running)
    if ($Running) { Remove-Item -LiteralPath $Running -Force -ErrorAction SilentlyContinue }
}
