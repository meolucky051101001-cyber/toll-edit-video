Set WshShell = CreateObject("WScript.Shell")
Set FileSystem = CreateObject("Scripting.FileSystemObject")
ScriptDirectory = FileSystem.GetParentFolderName(WScript.ScriptFullName)
WshShell.Run Chr(34) & ScriptDirectory & "\start_bot.bat" & Chr(34), 0
Set WshShell = Nothing
