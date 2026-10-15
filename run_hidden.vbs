Option Explicit

Dim shell, root, powershell, script, command
Set shell = CreateObject("WScript.Shell")
root = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
powershell = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")
script = root & "\bootstrap.ps1"
command = """" & powershell & """ -NoProfile -ExecutionPolicy Bypass -File """ & script & """ -Mode reader"
shell.Run command, 1, False
