Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")
ScriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)

' 1. Dam bao he thong chay ngam neu chua bat
WshShell.Run Chr(34) & ScriptDir & "\KhoiDongAn.vbs" & Chr(34), 0, False

' Cho 0.5s phong truong hop he thong vua khoi dong
WScript.Sleep 500

' 2. Mo giao dien Dashboard (Chrome app mode neu co, hoac trinh duyet mac dinh)
ChromePath = "C:\Program Files\Google\Chrome\Application\chrome.exe"
EdgePath = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

If FSO.FileExists(ChromePath) Then
    WshShell.Run Chr(34) & ChromePath & Chr(34) & " --app=http://127.0.0.1:8088/", 1, False
ElseIf FSO.FileExists(EdgePath) Then
    WshShell.Run Chr(34) & EdgePath & Chr(34) & " --app=http://127.0.0.1:8088/", 1, False
Else
    WshShell.Run "http://127.0.0.1:8088/", 1, False
End If

Set WshShell = Nothing
Set FSO = Nothing
