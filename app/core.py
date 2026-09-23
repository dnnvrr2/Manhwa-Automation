from __future__ import annotations
import json, uuid
from pathlib import Path
from .models import Caption, Panel, Project, TranscriptSegment, Word, EFFECTS

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

def parse_elevenlabs(path: str) -> tuple[str, list[Word]]:
    language, words, _ = parse_elevenlabs_with_segments(path)
    return language, words

def parse_elevenlabs_with_segments(path: str) -> tuple[str, list[Word], list[TranscriptSegment]]:
    try: data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e: raise ValueError(f"Cannot read JSON: {e}") from e
    words, segments = [], []
    for segment in data.get("segments", []):
        if all(key in segment for key in ("text", "start_time", "end_time")) and str(segment["text"]).strip():
            segments.append(TranscriptSegment(str(segment["text"]).strip(), float(segment["start_time"]), float(segment["end_time"])))
        for item in segment.get("words", []):
            text = str(item.get("text", ""))
            if text.strip() and "start_time" in item and "end_time" in item:
                words.append(Word(text.strip(), float(item["start_time"]), float(item["end_time"])))
    if not words: raise ValueError("No timestamped spoken words found. Export ElevenLabs JSON with timestamps enabled.")
    return str(data.get("language_code", "")), words, segments

def group_captions(words: list[Word], grouping: str = "auto", uppercase: bool = True) -> list[Caption]:
    size = {"2": 2, "3": 3, "4": 4}.get(str(grouping), 0)
    result, i = [], 0
    while i < len(words):
        take = size or (3 if i + 3 <= len(words) and not words[i + 1].text.endswith((".", "!", "?", ",")) else 2)
        batch = words[i:i + take]
        if len(batch) == 1 and result:
            previous = result.pop(); batch = previous.words + batch
        text = " ".join(w.text for w in batch)
        result.append(Caption(text.upper() if uppercase else text, batch[0].start, batch[-1].end, batch))
        i += take
    return result

def assign_timings(panels: list[Panel], words: list[Word], segments: list[TranscriptSegment] | None = None) -> None:
    if not panels: return
    if segments:
        for index, panel in enumerate(panels):
            if index < len(segments):
                panel.start_time, panel.end_time = segments[index].start, segments[index].end
            else:
                panel.start_time = panel.end_time = 0.0
        return
    duration = words[-1].end if words else len(panels) * 3.0
    targets = [duration * i / len(panels) for i in range(1, len(panels))]
    cuts = []
    for target in targets:
        candidates = [w.end for w in words if w.end >= 0.4 and w.end <= duration - 0.4]
        cuts.append(min(candidates, key=lambda x: abs(x - target)) if candidates else target)
    bounds = [0.0] + cuts + [duration]
    for panel, start, end in zip(panels, bounds, bounds[1:]): panel.start_time, panel.end_time = start, end

def safe_pan_bounds(source_width: int, source_height: int, output_width: int, output_height: int, zoom: float = 1.15) -> tuple[float, float]:
    """Return normalized (0..1) horizontal/vertical movement headroom after cover scaling."""
    if min(source_width, source_height, output_width, output_height) <= 0 or zoom < 1:
        raise ValueError("Dimensions must be positive and zoom must be at least 1.")
    cover = max(output_width / source_width, output_height / source_height)
    scaled_w, scaled_h = source_width * cover * zoom, source_height * cover * zoom
    return (max(0.0, (scaled_w - output_width) / scaled_w), max(0.0, (scaled_h - output_height) / scaled_h))

def assign_effects(panels: list[Panel]) -> None:
    for i, panel in enumerate(panels):
        if panel.effect_source == "auto": panel.effect = EFFECTS[i % len(EFFECTS)]

def set_effect(panels: list[Panel], index: int, effect: str) -> None:
    if effect not in EFFECTS: raise ValueError("Unknown effect")
    panels[index].effect, panels[index].effect_source = effect, "manual"

def save_project(project: Project, path: str) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(project.to_dict(), indent=2), encoding="utf-8")

def load_project(path: str) -> Project:
    return Project.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

def missing_sources(project: Project) -> list[str]:
    paths = [p.path for p in project.panels] + ([project.audio_path] if project.audio_path else [])
    return [path for path in paths if path and not Path(path).exists()]

def validate_render(project: Project) -> list[str]:
    problems = []
    if not project.panels: problems.append("Add at least one panel.")
    if not project.audio_path: problems.append("Import narration audio.")
    elif not Path(project.audio_path).exists(): problems.append("Narration audio file is missing.")
    if not project.words: problems.append("Import a timestamped ElevenLabs transcript.")
    if project.segments and len(project.panels) != len(project.segments):
        problems.append(f"Panel count ({len(project.panels)}) must match narration segment count ({len(project.segments)}).")
    problems.extend(f"Panel source is missing: {Path(path).name}" for path in missing_sources(project) if path != project.audio_path)
    if any(panel.duration <= 0 for panel in project.panels): problems.append("One or more panel durations are invalid. Recalculate timing.")
    return problems

def safe_rename(panels: list[Panel]) -> None:
    sources = {str(Path(p.path).resolve()) for p in panels}
    for panel in panels:
        source = Path(panel.path)
        destination = source.with_name(f"{panel.sequence:03d}{source.suffix.lower()}")
        if destination.exists() and str(destination.resolve()) not in sources:
            raise FileExistsError(f"Destination already exists: {destination}")
    staged = []
    for panel in panels:
        src = Path(panel.path)
        if not src.exists(): raise FileNotFoundError(src)
        temp = src.with_name(f".__manhwa_{uuid.uuid4().hex}{src.suffix}")
        src.rename(temp); staged.append((panel, src, temp))
    moved = []
    try:
        for panel, src, temp in staged:
            dest = temp.with_name(f"{panel.sequence:03d}{temp.suffix.lower()}")
            temp.rename(dest); moved.append((panel, src, dest)); panel.path = str(dest)
    except Exception:
        for panel, src, dest in reversed(moved):
            if dest.exists(): dest.rename(src); panel.path = str(src)
        for panel, src, temp in staged:
            if temp.exists(): temp.rename(src); panel.path = str(src)
        raise

def edit_plan(project: Project) -> dict:
    duration = project.words[-1].end if project.words else max((p.end_time for p in project.panels), default=0)
    return {"duration": duration, "panels": [{"id":p.id,"sequence":p.sequence,"file":p.path,"start":p.start_time,"end":p.end_time,"effect":p.effect,"zoom_anchor":p.zoom_anchor} for p in project.panels], "captions": [{"text":c.text,"start":c.start,"end":c.end,"words":[w.__dict__ for w in c.words]} for c in project.captions]}

def write_ass(project: Project, path: str) -> None:
    s = project.caption_settings
    def color(value: str, fallback: str) -> str:
        value = str(value or fallback).upper().replace("#", "")
        return value.zfill(6)[-6:]
    normal = color(s.normal_color, "FFFFFF")
    highlight = color(s.highlight_color, "00FFFF")
    outline_color = color(s.outline_color, "000000")
    shadow_color = color(s.shadow_color, "000000")
    alignment = {"left": 1, "center": 2, "right": 3}.get(s.horizontal_alignment, 2)
    margin = max(0, int(s.horizontal_margin))
    vertical_margin = max(0, min(1920, 1920 - int(s.vertical_position)))
    weight = {"Regular": 400, "Medium": 500, "Bold": 700, "Extra Bold": 800}.get(s.font_weight, 700)
    lines = ["[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920", "[V4+ Styles]", "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding", f"Style: Default,{s.font},{s.font_size},&H00{normal},&H00{highlight},&H00{outline_color},&H00{shadow_color},0,0,0,0,100,100,0,0,1,{s.outline},{s.shadow},{alignment},{margin},{margin},{vertical_margin},1", "[Events]", "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text"]
    def ts(v):
        h=int(v//3600); m=int(v%3600//60); sec=v%60; return f"{h}:{m:02}:{sec:05.2f}"
    for caption in project.captions:
        # Keep the caption visible across true pauses, changing only its active-word colour.
        boundaries = sorted({max(0.0, t + s.timing_offset) for t in (caption.start, caption.end, *(t for word in caption.words for t in (word.start, word.end)))})
        for start, end in zip(boundaries, boundaries[1:]):
            if end <= start: continue
            midpoint = (start + end) / 2
            active = next((word for word in caption.words if word.start + s.timing_offset <= midpoint <= word.end + s.timing_offset), None)
            words = [word.text.upper() if s.all_caps else word.text for word in caption.words]
            if s.highlight_mode == "line":
                text = f"{{\\b{weight}\\c&H{highlight}&}}{' '.join(words)}"
            else:
                text = " ".join(f"{{\\c&H{highlight}&}}{word}{{\\c&H{normal}&}}" if source is active else word for word, source in zip(words, caption.words))
                text = f"{{\\b{weight}}}{text}"
            lines.append(f"Dialogue: 0,{ts(start)},{ts(end)},Default,,0,0,0,,{text}")
    Path(path).write_text("\n".join(lines), encoding="utf-8-sig")
