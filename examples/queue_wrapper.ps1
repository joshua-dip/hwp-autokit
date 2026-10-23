# Wrap any Hangul step of a PowerShell pipeline in the shared build queue.
#
#   powershell -ExecutionPolicy Bypass -File examples\queue_wrapper.ps1 -Tag job42 -- python build.py job42
param(
    [Parameter(Mandatory)][string]$Tag,
    [string]$QueueDir = (Join-Path $env:TEMP 'hwp-autokit-queue'),
    [int]$Priority = 5,
    [Parameter(ValueFromRemainingArguments)][string[]]$Command
)
. (Join-Path $PSScriptRoot '..\scripts\hwp_queue.ps1')

$t = Enter-HwpQueue -QueueDir $QueueDir -Tag $Tag -Priority $Priority -LogFile (Join-Path $QueueDir "$Tag.log")
try {
    & $Command[0] $Command[1..($Command.Count - 1)]
    exit $LASTEXITCODE
} finally {
    Exit-HwpQueue $t
}
