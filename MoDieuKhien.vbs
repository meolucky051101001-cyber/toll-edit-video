Set shell = CreateObject("WScript.Shell")
shell.Run """C:\tool v1\backend\venv\Scripts\pythonw.exe"" ""C:\tool v1\backend\tool_control.py""", 0, False
WScript.Sleep 1500
shell.Run "http://127.0.0.1:8090/"
