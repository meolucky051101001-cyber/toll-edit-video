Option Explicit
Dim shell, files, root, python, pythonw, command, environment
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
Set environment = shell.Environment("PROCESS")
python = environment("AUTODUB_CLOUD_PYTHON")
If WScript.Arguments.Count > 0 Then python = WScript.Arguments(0)
python = Trim(Replace(python, Chr(34), ""))
If python = "" Then python = files.BuildPath(root, "backend\venv\Scripts\python.exe")
environment("AUTODUB_CLOUD_PYTHON") = python
pythonw = files.BuildPath(files.GetParentFolderName(python), "pythonw.exe")
If Not files.FileExists(python) Or Not files.FileExists(pythonw) Then
    MsgBox "Khong tim thay Python Tool V1. Kiem tra AUTODUB_CLOUD_PYTHON hoac backend\venv.", 16, "Tool V1 Cloud"
    WScript.Quit 1
End If
If environment("AUTODUB_CLOUD_OUTPUT_DIR") = "" Then environment("AUTODUB_CLOUD_OUTPUT_DIR") = "D:\ToolV1_Cloud"
shell.CurrentDirectory = root
command = Chr(34) & pythonw & Chr(34) & " -B -X utf8 " & Chr(34) & files.BuildPath(root, "launch_cloud_dashboard.py") & Chr(34)
shell.Run command, 0, False
WScript.Quit 0
