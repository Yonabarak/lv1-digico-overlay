' Launch LV1 overlay without a console window.
' Use project venv pythonw — PyManager re-execs via Windows Store alias (second overlay).
Set shell = CreateObject("Wscript.Shell")
pyw = "E:\OSC2\.venv\Scripts\pythonw.exe"
shell.Run """" & pyw & """ ""E:\OSC2\lv1_overlay.py""", 0, False
