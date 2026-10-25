Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.Run "cmd /c (where pyw >nul 2>nul && pyw -3 bridge.py) || pythonw bridge.py", 0, False
