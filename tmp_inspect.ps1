$ErrorActionPreference = 'SilentlyContinue'

Write-Output '=== TeShareCloud processes ==='
Get-Process | Where-Object { $_.Path -like '*TeShareCloud*' } | Select-Object Id, ProcessName, Path | Format-Table -AutoSize | Out-String -Width 200

Write-Output '=== All pxClient / px* processes ==='
Get-Process | Where-Object { $_.ProcessName -like 'px*' } | Select-Object Id, ProcessName, Path | Format-Table -AutoSize | Out-String -Width 200

Write-Output '=== Established outbound TCP (unique, with process names) ==='
Get-NetTCPConnection -State Established | Where-Object { $_.RemoteAddress -notlike '127.*' -and $_.RemoteAddress -ne '::1' } |
  ForEach-Object {
    $p = Get-Process -Id $_.OwningProcess
    '{0,-8} {1,-20} -> {2}:{3}' -f $_.OwningProcess, $p.ProcessName, $_.RemoteAddress, $_.RemotePort
  } | Sort-Object -Unique
