Option Explicit

Dim WshShell, FSO, ScriptDir, CmdPath, Url, HealthUrl
Dim ChromePath, ChromePathX86, EdgePath, EdgePath64
Dim i

Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")
ScriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)
Url = "http://127.0.0.1:8088/"
HealthUrl = "http://127.0.0.1:8088/api/health"

Function IsDashboardReady(checkUrl)
    Dim req
    IsDashboardReady = False
    On Error Resume Next
    Set req = CreateObject("WinHttp.WinHttpRequest.5.1")
    req.SetProxy 1
    req.SetTimeouts 500, 500, 500, 1000
    req.Open "GET", checkUrl, False
    req.Send
    If Err.Number = 0 Then
        If req.Status >= 200 And req.Status < 400 Then
            IsDashboardReady = True
        End If
    End If
    Err.Clear
    On Error GoTo 0
    Set req = Nothing
End Function

' 1. Chi khoi dong he thong ngam neu Dashboard (cong 8088) chua chay.
'    Goi qua cmd.exe (Zone 0) thay vi ShellExecute truc tiep .vbs/.bat
'    de tranh popup "Open File - Security Warning".
If Not IsDashboardReady(HealthUrl) Then
    CmdPath = WshShell.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\cmd.exe"
    WshShell.Run Chr(34) & CmdPath & Chr(34) & " /c call " & Chr(34) & ScriptDir & "\start_bot.bat" & Chr(34), 0, False
    For i = 1 To 20
        WScript.Sleep 500
        If IsDashboardReady(HealthUrl) Then Exit For
    Next
End If

' 2. Mo giao dien Dashboard (Chrome app mode neu co, hoac Edge / trinh duyet mac dinh)
ChromePath = "C:\Program Files\Google\Chrome\Application\chrome.exe"
ChromePathX86 = "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
EdgePath = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
EdgePath64 = "C:\Program Files\Microsoft\Edge\Application\msedge.exe"

If FSO.FileExists(ChromePath) Then
    WshShell.Run Chr(34) & ChromePath & Chr(34) & " --app=" & Url, 1, False
ElseIf FSO.FileExists(ChromePathX86) Then
    WshShell.Run Chr(34) & ChromePathX86 & Chr(34) & " --app=" & Url, 1, False
ElseIf FSO.FileExists(EdgePath) Then
    WshShell.Run Chr(34) & EdgePath & Chr(34) & " --app=" & Url, 1, False
ElseIf FSO.FileExists(EdgePath64) Then
    WshShell.Run Chr(34) & EdgePath64 & Chr(34) & " --app=" & Url, 1, False
Else
    WshShell.Run "rundll32 url.dll,FileProtocolHandler " & Url, 1, False
End If

Set WshShell = Nothing
Set FSO = Nothing
