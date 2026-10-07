param([string]$TextFile,[int]$X,[int]$Y,[int]$ParentPid,[string]$ReadyFile)
# Small always-on-top instruction box that never takes the keyboard focus. Python writes the text file;
# the box closes when the file contains __CLOSE__ or when the parent process has exited.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -ReferencedAssemblies System.Windows.Forms,System.Drawing -TypeDefinition @"
using System.Windows.Forms;
public class TeachForm : Form {
  protected override bool ShowWithoutActivation { get { return true; } }
  protected override CreateParams CreateParams {
    get { CreateParams p = base.CreateParams; p.ExStyle |= 0x08000000 | 0x80; return p; }
  }
}
"@
$form = New-Object TeachForm
$form.FormBorderStyle = 'None'
$form.StartPosition = 'Manual'
$form.Location = New-Object System.Drawing.Point($X, $Y)
$form.Size = New-Object System.Drawing.Size(780, 86)
$area = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
$form.Location = New-Object System.Drawing.Point([Math]::Max($area.Left, [Math]::Min($X, $area.Right - $form.Width)), [Math]::Max($area.Top, [Math]::Min($Y, $area.Bottom - $form.Height)))
$form.TopMost = $true
$form.ShowInTaskbar = $false
$form.BackColor = [System.Drawing.Color]::FromArgb(27, 36, 52)
$label = New-Object System.Windows.Forms.Label
$label.Dock = 'Fill'
$label.Padding = New-Object System.Windows.Forms.Padding(12, 8, 12, 8)
$label.ForeColor = [System.Drawing.Color]::White
$label.Font = New-Object System.Drawing.Font('Meiryo UI', 11)
$form.Controls.Add($label)
$form.Add_Shown({
 if ($ReadyFile) { [System.IO.File]::WriteAllText($ReadyFile, 'ready') }
})
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 150
$last = ''
$timer.Add_Tick({
  try {
    $text = [System.IO.File]::ReadAllText($TextFile, [System.Text.Encoding]::UTF8)
  } catch { return }
  if ($text -eq '__CLOSE__' -or -not (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)) { $form.Close(); return }
  if ($text -ne $script:last) { $script:last = $text; $label.Text = $text }
})
$timer.Start()
[System.Windows.Forms.Application]::Run($form)
