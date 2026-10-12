' Starts Local Cinema in the background (no window).
' Put a shortcut to this file in the Windows Startup folder (Win+R, shell:startup) to run it at login.
'
' Settings: change these to match your setup.
moviesFolder = "E:\Movies"
port = "8000"
' python.exe (not pythonw.exe) so an existing Windows Firewall rule for python.exe applies.
' Use a full path here if "python" is not on your PATH.
python = "python"

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
sh.Run """" & python & """ """ & dir & "\movie_server.py"" """ & moviesFolder & """ " & port, 0, False
