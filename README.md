# Discord TTS Microphone

## Desktop app

Requires Python 3.10 or newer on Windows.

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python desktop_app.py
```

Choose one or two output devices in the app. To send TTS to Discord through a
virtual audio cable, select the cable's playback device here and select its
matching recording device as Discord's input. Two outputs play at nearly the
same time; a small timing difference between devices is possible.

The desktop app uses Windows audio devices directly. `server.py` and
`index.html` remain available for the browser version.