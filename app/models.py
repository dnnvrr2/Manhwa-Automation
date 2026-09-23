from __future__ import annotations
from dataclasses import asdict, dataclass, field
from pathlib import Path
from uuid import uuid4

EFFECTS = ("zoom_in", "slide_left", "slide_right", "slide_up", "slide_down")
ANCHORS = ("center", "top", "bottom", "left", "right", "custom")

@dataclass
class Word:
    text: str
    start: float
    end: float

@dataclass
class TranscriptSegment:
    text: str
    start: float
    end: float

@dataclass
class Caption:
    text: str
    start: float
    end: float
    words: list[Word]

@dataclass
class Panel:
    path: str
    original_file: str
    id: str = field(default_factory=lambda: str(uuid4()))
    sequence: int = 0
    start_time: float = 0.0
    end_time: float = 0.0
    effect: str = "zoom_in"
    effect_source: str = "auto"
    zoom_anchor: str = "center"
    crop_path: str = ""
    crop_rect: tuple[float, float, float, float] | None = None

    @property
    def duration(self) -> float: return max(0.0, self.end_time - self.start_time)
    @property
    def file(self) -> str: return Path(self.path).name
    @property
    def render_path(self) -> str: return self.crop_path or self.path

@dataclass
class CaptionSettings:
    font: str = "Arial"
    font_size: int = 64
    vertical_position: int = 1450
    normal_color: str = "FFFFFF"
    highlight_color: str = "00FFFF"  # ASS BGR: yellow
    outline: int = 5
    shadow: int = 2
    all_caps: bool = True
    grouping: str = "auto"
    horizontal_alignment: str = "center"
    horizontal_margin: int = 50
    font_weight: str = "Bold"
    outline_color: str = "000000"
    shadow_color: str = "000000"
    timing_offset: float = 0.0
    highlight_mode: str = "word"

@dataclass
class Project:
    name: str = "Untitled Project"
    resolution: tuple[int, int] = (1080, 1920)
    fps: int = 30
    panels: list[Panel] = field(default_factory=list)
    audio_path: str = ""
    transcript_path: str = ""
    words: list[Word] = field(default_factory=list)
    segments: list[TranscriptSegment] = field(default_factory=list)
    captions: list[Caption] = field(default_factory=list)
    caption_settings: CaptionSettings = field(default_factory=CaptionSettings)
    background_music_path: str = ""
    background_music_volume: float = 0.16
    render_settings: dict = field(default_factory=dict)

    def renumber(self) -> None:
        for i, panel in enumerate(self.panels, 1): panel.sequence = i

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Project":
        project = cls(name=data.get("name", "Untitled Project"), resolution=tuple(data.get("resolution", [1080, 1920])), fps=data.get("fps", 30))
        project.panels = [Panel(**p) for p in data.get("panels", [])]
        project.audio_path, project.transcript_path = data.get("audio_path", ""), data.get("transcript_path", "")
        project.words = [Word(**w) for w in data.get("words", [])]
        project.segments = [TranscriptSegment(**segment) for segment in data.get("segments", [])]
        project.captions = [Caption(words=[Word(**w) for w in c["words"]], **{k:v for k,v in c.items() if k != "words"}) for c in data.get("captions", [])]
        project.caption_settings = CaptionSettings(**data.get("caption_settings", {}))
        project.background_music_path = data.get("background_music_path", "")
        project.background_music_volume = float(data.get("background_music_volume", 0.16))
        project.render_settings = data.get("render_settings", {})
        project.renumber(); return project
