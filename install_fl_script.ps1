# Copies the EFLC bridge script into FL Studio's user hardware-script folder.
# Re-run after editing fl_script\EFLC\device_EFLC.py, then in FL:
# Options > MIDI settings > select the EFLC input > "Reload script" (or restart FL).

$src = Join-Path $PSScriptRoot "fl_script\EFLC"
$dst = Join-Path ([Environment]::GetFolderPath("MyDocuments")) "Image-Line\FL Studio\Settings\Hardware\EFLC"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item -Path (Join-Path $src "*.py") -Destination $dst -Force
Write-Host "Installed EFLC bridge to $dst"
