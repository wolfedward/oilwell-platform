Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "C:\Users\hp\Documents\外专双百"
shell.Run """C:\Users\hp\Documents\外专双百\.venv\Scripts\python.exe"" -m backend.center --config config\center.local.toml", 0, False
