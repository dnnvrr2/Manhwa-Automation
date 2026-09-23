from __future__ import annotations
import os, subprocess, sys, tempfile, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from .core import write_ass
from .models import Project
from .core import missing_sources, validate_render

class RenderError(RuntimeError): pass

DEFAULT_BGM_RELATIVE_PATH = Path("assets") / "background_music.mp3"
DEFAULT_BGM_VOLUME = 0.16

def default_background_music_path() -> Path | None:
    roots = [Path.cwd(), Path(sys.executable).resolve().parent, Path(__file__).resolve().parent.parent]
    for root in roots:
        candidate = root / DEFAULT_BGM_RELATIVE_PATH
        if candidate.is_file(): return candidate
    return None

def background_music_path(project: Project) -> Path | None:
    if project.background_music_path:
        selected=Path(project.background_music_path)
        return selected if selected.is_file() else None
    return default_background_music_path()

def background_music_volume(project: Project) -> float:
    return max(0.0,min(1.0,float(getattr(project,"background_music_volume",DEFAULT_BGM_VOLUME))))

class _CancelGroup:
    def __init__(self, *events): self.events = [event for event in events if event is not None]
    def is_set(self): return any(event.is_set() for event in self.events)

def panel_render_workers(panel_count: int) -> int:
    """Use two clip jobs on CPUs with enough logical threads, otherwise keep one job."""
    if panel_count < 2: return 1
    return min(panel_count, 2 if (os.cpu_count() or 1) >= 6 else 1)

def render(project: Project, output: str, preview: bool = False, progress=None, cancel: threading.Event | None = None) -> None:
    problems = preview_validation(project) if preview else validate_render(project)
    if problems: raise RenderError("\n".join(problems))
    if not shutil_which("ffmpeg"): raise RenderError("FFmpeg was not found on PATH.")
    width, height = (240, 426) if preview else project.resolution
    with tempfile.TemporaryDirectory(prefix="manhwa_render_") as work:
        clips = []; jobs = []; video_duration = 0.0
        for i, panel in enumerate(project.panels):
            duration = panel_render_duration(project, i); clip = Path(work) / f"{i:04}.mp4"; clips.append(clip)
            video_duration += duration
            jobs.append((panel, duration, clip))
        workers = panel_render_workers(len(jobs)); threads = max(1, (os.cpu_count() or 1) // workers)
        internal_cancel = threading.Event(); clip_cancel = _CancelGroup(cancel, internal_cancel)
        def render_clip(job):
            panel, duration, clip = job
            # Stable frame input plus larger, smooth motion rendered via zoompan.
            vf = motion_filter(panel.effect, panel.zoom_anchor, width, height, project.fps, duration)
            cmd = ["ffmpeg", "-y", "-threads", str(threads), "-framerate", str(project.fps), "-loop", "1", "-i", panel.render_path, "-t", f"{duration:.5f}", "-vf", vf, "-r", str(project.fps), "-an", "-c:v", "libx264", "-preset", "veryfast" if preview else "medium", "-pix_fmt", "yuv420p", str(clip)]
            _run(cmd, clip_cancel)
        completed = 0
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="panel_render") as executor:
            futures = [executor.submit(render_clip, job) for job in jobs]
            try:
                for future in as_completed(futures):
                    future.result(); completed += 1
                    if progress: progress(completed / (len(project.panels) + 2) * 80)
            except Exception:
                internal_cancel.set()
                for future in futures: future.cancel()
                raise
        concat = Path(work) / "list.txt"; concat.write_text("\n".join(f"file '{p.as_posix()}'" for p in clips), encoding="utf-8")
        video = Path(work) / "video.mp4"; _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(video)], cancel)
        command = ["ffmpeg", "-y", "-i", str(video)]
        has_voice = bool(project.audio_path and Path(project.audio_path).exists())
        background_music = background_music_path(project); music_volume = background_music_volume(project)
        if has_voice: command.extend(["-i", project.audio_path])
        if background_music: command.extend(["-stream_loop", "-1", "-i", str(background_music)])
        if project.captions:
            ass = Path(work) / "captions.ass"; write_ass(project, str(ass)); ass_path = str(ass).replace("\\", "/").replace(":", "\\:")
            command.extend(["-vf", f"ass='{ass_path}'"])
        command.extend(["-c:v", "libx264", "-preset", "veryfast" if preview else "slow", "-crf", "30" if preview else "18"])
        if background_music:
            if has_voice:
                command.extend(["-filter_complex", f"[1:a]volume=1.0[voice];[2:a]volume={music_volume:.3f}[bgm];[voice][bgm]amix=inputs=2:duration=longest:dropout_transition=0[audio]", "-map", "0:v:0", "-map", "[audio]"])
            else:
                command.extend(["-filter:a", f"volume={music_volume:.3f}", "-map", "0:v:0", "-map", "1:a:0"])
            command.extend(["-c:a", "aac", "-t", f"{video_duration:.5f}"])
        elif has_voice: command.extend(["-c:a", "aac", "-shortest"])
        command.extend(["-movflags", "+faststart", output]); _run(command, cancel)
        if progress: progress(100)

def panel_render_duration(project: Project, index: int) -> float:
    """Keep segment pauses in the output timeline instead of concatenating them away."""
    panel = project.panels[index]
    if not any(item.duration > 0 for item in project.panels):
        return 3.0
    timeline_start = 0.0 if index == 0 else panel.start_time
    if index + 1 < len(project.panels):
        timeline_end = project.panels[index + 1].start_time
    else:
        narration_end = project.words[-1].end if project.words else panel.end_time
        timeline_end = max(panel.end_time, narration_end)
    return max(.1, timeline_end - timeline_start)

def preview_validation(project: Project) -> list[str]:
    if not project.panels:
        return ["Add at least one panel before rendering a preview."]
    return [f"Panel source is missing: {Path(path).name}" for path in missing_sources(project) if path != project.audio_path]

def motion_filter(effect, anchor, w, h, fps, duration):
    frames = max(1, round(duration * fps))
    frame_count = max(1, frames - 1)
    progress = f"(on/{frame_count})"
    eased = f"({progress})*({progress})*(3-2*({progress}))"
    z = f"1+0.08*{progress}" if effect == "zoom_in" else "1.15"
    x = "(iw-iw/zoom)/2"; y = "(ih-ih/zoom)/2"
    if effect == "slide_left": x = f"(iw-iw/zoom)*(1-{eased})"
    if effect == "slide_right": x = f"(iw-iw/zoom)*{eased}"
    if effect == "slide_up": y = f"(ih-ih/zoom)*(1-{eased})"
    if effect == "slide_down": y = f"(ih-ih/zoom)*{eased}"
    # Work on a 4x canvas so zoompan's whole-pixel rounding is invisible.
    big_w, big_h = w * 4, h * 4
    scale = (f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase:flags=lanczos,"
             f"crop={big_w}:{big_h}")
    return f"{scale},zoompan=z='{z}':x='{x}':y='{y}':d=1:s={w}x{h}:fps={fps}"

def _run(cmd, cancel=None):
    # FFmpeg's default progress stream can fill a pipe during long renders;
    # limit it to errors so the worker never deadlocks while polling cancellation.
    if cancel and cancel.is_set(): raise RenderError("Rendering cancelled.")
    command = [cmd[0], "-loglevel", "error", *cmd[1:]]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    while process.poll() is None:
        if cancel and cancel.is_set():
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
            raise RenderError("Rendering cancelled.")
        threading.Event().wait(.1)
    stderr = process.stderr.read() if process.stderr else ""
    if process.returncode: raise RenderError(stderr[-1500:])

def shutil_which(name):
    import shutil; return shutil.which(name)
