Set shell = CreateObject("WScript.Shell")
scriptPath = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
projectRoot = CreateObject("Scripting.FileSystemObject").GetParentFolderName(scriptPath)
command = "powershell -NoProfile -ExecutionPolicy Bypass -File """ & projectRoot & "\scripts\startup-launch.ps1"""
shell.Run command, 0, False
