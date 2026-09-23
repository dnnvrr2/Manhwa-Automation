# Manhwa TikTok Automation

Windows desktop editor for arranging manhwa panels, importing ElevenLabs word timestamps, creating word-highlighted captions, and rendering vertical recap videos with FFmpeg.

## Run

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

FFmpeg must be on `PATH` for rendering. Run tests with `python -m unittest discover -s tests`.

## Default background music

Place your track at `assets/background_music.mp3` to use it as the default. You can also choose another track and adjust its volume in Settings & Details. The selected track is saved with the project and plays in the live timeline as well as rendered videos.

The project autosaves beside its project JSON. Package a Windows executable with `./build_windows.ps1`.
