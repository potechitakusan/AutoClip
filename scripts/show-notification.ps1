param([string]$Title,[string]$Message)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$notice=New-Object System.Windows.Forms.NotifyIcon
try {
 $notice.Icon=[System.Drawing.SystemIcons]::Information
 $notice.Text='AutoClip'
 $notice.Visible=$true
 $notice.ShowBalloonTip(6000,$Title,$Message,[System.Windows.Forms.ToolTipIcon]::Info)
 $until=[DateTime]::UtcNow.AddSeconds(8)
 while ([DateTime]::UtcNow -lt $until) {
  [System.Windows.Forms.Application]::DoEvents()
  Start-Sleep -Milliseconds 100
 }
} finally {
 $notice.Visible=$false
 $notice.Dispose()
}
