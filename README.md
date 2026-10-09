# Discord TTS Microphone

## Desktop app

Requires Python 3.10 or newer on Windows.

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python desktop_app.py   
```

To enable offline Thai Piper, install its phonemizer dependencies and TLTK:

```powershell
python -m pip install -r requirements-piper-thai.txt
python -m pip install --no-deps tltk==1.11
```

For 9Router, start its local API on port `20128` and set `NINE_ROUTER_API_KEY`
in the environment before launching the app. Keep the token out of source files
and shell scripts.

The separate TLTK install avoids its old `scikit-learn~=1.2` pin, which has no
Python 3.13 wheel; the optional requirements install a newer version used by
Piper's Thai phonemizer. The app downloads the Thai Piper model (about 63 MB) on
first use and caches it under `%LOCALAPPDATA%\DiscordTTS\piper`. First use can
take longer while the model loads. The model is licensed CC BY-NC-SA 3.0 and is
for non-commercial use only. `pip-audit` currently flags NLTK 3.10.3
(GHSA-8mgp-746c-j5xp); no patched release is available yet. Piper uses TLTK's
Thai phonemizer here, while this app does not call the affected NLTK model-file
APIs.

Choose the voice source and voice, then select one or two output devices. Edge
TTS and Google TTS require an internet connection; Google TTS adds a Thai
fallback through gTTS and Google Translate's undocumented speech endpoint (not
Google Cloud TTS), which may change or become unavailable. Piper provides
offline Thai speech after its model is downloaded. For mixed Thai-English text,
choose `Piper ไทย + Windows English`; Thai and English sections use separate
offline voices. Windows Speech uses voices installed in Windows and works offline.
To send TTS to Discord through a
virtual audio cable, select the cable's playback device here and select its
matching recording device as Discord's input. Two outputs play at nearly the
same time; a small timing difference between devices is possible.

To relay another program's audio, set `Capture Mode` to `โปรแกรม (process tree)`,
refresh the process list, select its PID, choose the destination under `Output
Device 1/2`, then start the relay. This uses Windows Process Loopback and
captures that process and its child processes; it requires Windows 10 build
20348 or newer. Browser audio is selected at process level, not reliably per
tab, because multiple tabs may share renderer processes. `Playback device
ทั้งหมด` remains available as a device-wide fallback.

The desktop app uses Windows audio devices directly. `server.py` and
`index.html` remain available for the browser version.