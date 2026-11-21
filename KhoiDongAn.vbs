Set WshShell = CreateObject("WScript.Shell")
Set FileSystem = CreateObject("Scripting.FileSystemObject")
ScriptDirectory = FileSystem.GetParentFolderName(WScript.ScriptFullName)
CmdPath = WshShell.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\cmd.exe"
WshShell.Run Chr(34) & CmdPath & Chr(34) & " /c call " & Chr(34) & ScriptDirectory & "\start_bot.bat" & Chr(34), 0, False
Set WshShell = Nothing
Set FileSystem = Nothing
