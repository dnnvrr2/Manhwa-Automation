import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app.core import assign_effects, assign_timings, group_captions, load_project, parse_elevenlabs, parse_elevenlabs_with_segments, safe_pan_bounds, safe_rename, save_project, set_effect, validate_render, write_ass
from app.models import CaptionSettings, EFFECTS, Panel, Project, TranscriptSegment, Word
from app.renderer import panel_render_duration
from app import renderer

class CoreTests(unittest.TestCase):
    def test_parser_ignores_whitespace_and_preserves_gap(self):
        data={"language_code":"eng","segments":[{"words":[{"text":"Hello","start_time":0.1,"end_time":0.3},{"text":" ","start_time":0.3,"end_time":0.4},{"text":"world","start_time":1.2,"end_time":1.5}]}]}
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"t.json"; path.write_text(json.dumps(data)); language,words=parse_elevenlabs(str(path))
        self.assertEqual(language,"eng"); self.assertEqual([w.text for w in words],["Hello","world"]); self.assertEqual(words[1].start,1.2)
    def test_caption_timing_uses_words(self):
        words=[Word("one",.1,.2),Word("two",.3,.4),Word("three",.9,1.1)]
        captions=group_captions(words,"3")
        self.assertEqual((captions[0].start,captions[0].end),(.1,1.1))
    def test_effect_cycle_and_manual_duplicates(self):
        panels=[Panel("x","x") for _ in range(5)]; assign_effects(panels)
        self.assertEqual([p.effect for p in panels],list(EFFECTS))
        set_effect(panels,2,"slide_left")
        self.assertEqual(panels[2].effect,"slide_left"); self.assertEqual(panels[2].effect_source,"manual")
        self.assertEqual(panels[1].effect,"slide_left"); self.assertEqual(panels[1].effect_source,"auto")
        panels.extend([Panel("x","x"),Panel("x","x")]); assign_effects(panels)
        self.assertEqual(panels[2].effect,"slide_left"); self.assertEqual([panel.effect for panel in panels[5:]],list(EFFECTS[:2]))
    def test_zoom_does_not_swap(self):
        panels=[Panel("x","x") for _ in range(3)]; assign_effects(panels); old=panels[1].effect
        set_effect(panels,0,"slide_right")
        self.assertEqual(panels[1].effect,old)
    def test_safe_pan_bounds_are_nonnegative(self):
        horizontal, vertical = safe_pan_bounds(600, 1200, 1080, 1920)
        self.assertGreaterEqual(horizontal, 0); self.assertGreaterEqual(vertical, 0)
    def test_project_round_trip(self):
        panel=Panel("image.jpg","original.jpg",crop_path="cache/cropped.png",crop_rect=(.1,.2,.7,.6))
        project=Project(name="Chapter 12",panels=[panel],words=[Word("Hi",0,1)],background_music_path="music.mp3",background_music_volume=.32)
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"project.json"; save_project(project,str(path)); restored=load_project(str(path))
        self.assertEqual(restored.name,"Chapter 12"); self.assertEqual(restored.panels[0].original_file,"original.jpg"); self.assertEqual(restored.panels[0].crop_path,"cache/cropped.png"); self.assertEqual(restored.panels[0].crop_rect,[.1,.2,.7,.6]); self.assertEqual(restored.panels[0].render_path,"cache/cropped.png"); self.assertEqual(restored.background_music_path,"music.mp3"); self.assertAlmostEqual(restored.background_music_volume,.32)
    def test_project_round_trip_preserves_caption_adjustments(self):
        project=Project(caption_settings=CaptionSettings(font="Impact",font_size=88,vertical_position=1510,normal_color="112233",highlight_color="445566",outline=7,shadow=4,horizontal_alignment="right",horizontal_margin=123,font_weight="Extra Bold",outline_color="010203",shadow_color="0A0B0C",timing_offset=.35,highlight_mode="line"))
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"project.json"; save_project(project,str(path)); restored=load_project(str(path))
        self.assertEqual(restored.caption_settings.horizontal_alignment,"right")
        self.assertEqual(restored.caption_settings.horizontal_margin,123)
        self.assertEqual(restored.caption_settings.font_weight,"Extra Bold")
        self.assertEqual(restored.caption_settings.outline_color,"010203")
        self.assertEqual(restored.caption_settings.shadow_color,"0A0B0C")
        self.assertAlmostEqual(restored.caption_settings.timing_offset,.35)
        self.assertEqual(restored.caption_settings.highlight_mode,"line")
    def test_old_project_uses_new_caption_defaults(self):
        old={"name":"Old project","caption_settings":{"font":"Arial","font_size":64,"vertical_position":1450,"normal_color":"FFFFFF","highlight_color":"00FFFF","outline":5,"shadow":2,"all_caps":True,"grouping":"auto"}}
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"old-project.json"; path.write_text(json.dumps(old)); project=load_project(str(path))
        self.assertEqual(project.caption_settings.horizontal_alignment,"center")
        self.assertEqual(project.caption_settings.horizontal_margin,50)
        self.assertEqual(project.caption_settings.font_weight,"Bold")
        self.assertEqual(project.caption_settings.outline_color,"000000")
        self.assertEqual(project.caption_settings.shadow_color,"000000")
        self.assertEqual(project.caption_settings.timing_offset,0.0)
        self.assertEqual(project.caption_settings.highlight_mode,"word")
        self.assertEqual(project.background_music_path,"")
        self.assertAlmostEqual(project.background_music_volume,.16)
    def test_old_panel_data_uses_uncropped_source(self):
        old={"name":"Old project","panels":[{"path":"original.png","original_file":"original.png"}]}
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"old-project.json"; path.write_text(json.dumps(old)); project=load_project(str(path))
        panel=project.panels[0]
        self.assertEqual(panel.crop_path,"")
        self.assertIsNone(panel.crop_rect)
        self.assertEqual(panel.render_path,"original.png")
    def test_write_ass_uses_caption_adjustments(self):
        words=[Word("hello",.5,1.0)]
        settings=CaptionSettings(font="Impact",font_size=88,vertical_position=1510,normal_color="112233",highlight_color="445566",outline=7,shadow=4,all_caps=True,horizontal_alignment="right",horizontal_margin=123,font_weight="Extra Bold",outline_color="010203",shadow_color="0A0B0C",highlight_mode="line")
        project=Project(words=words,captions=group_captions(words,"auto",True),caption_settings=settings)
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"captions.ass"; write_ass(project,str(path)); ass=path.read_text(encoding="utf-8-sig")
        self.assertIn("PlayResX: 1080",ass)
        self.assertIn("PlayResY: 1920",ass)
        self.assertIn("Style: Default,Impact,88,&H00112233,&H00445566,&H00010203,&H000A0B0C,0,0,0,0,100,100,0,0,1,7,4,3,123,123,410,1",ass)
        self.assertIn("{\\b800\\c&H445566&}HELLO",ass)
    def test_write_ass_applies_caption_timing_offset(self):
        words=[Word("hello",.5,1.0)]
        project=Project(words=words,captions=group_captions(words),caption_settings=CaptionSettings(timing_offset=.25))
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"captions.ass"; write_ass(project,str(path)); ass=path.read_text(encoding="utf-8-sig")
        self.assertIn("Dialogue: 0,0:00:00.75,0:00:01.25",ass)
    def test_safe_rename_uses_sequences(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); first=root/"page_a.jpg"; second=root/"page_b.png"; first.write_bytes(b"a"); second.write_bytes(b"b")
            panels=[Panel(str(second),second.name,sequence=1),Panel(str(first),first.name,sequence=2)]
            safe_rename(panels)
            self.assertTrue((root/"001.png").exists()); self.assertTrue((root/"002.jpg").exists())
    def test_segment_times_drive_panel_timing(self):
        data={"segments":[{"text":"First narration.","start_time":.72,"end_time":8.6,"words":[{"text":"First","start_time":.72,"end_time":1.0}]},{"text":"Second narration.","start_time":9.66,"end_time":12.52,"words":[{"text":"Second","start_time":9.66,"end_time":10.0}]}]}
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"segments.json"; path.write_text(json.dumps(data)); _,words,segments=parse_elevenlabs_with_segments(str(path))
        panels=[Panel("one","one"),Panel("two","two")]; assign_timings(panels,words,segments)
        self.assertEqual((panels[0].start_time,panels[0].end_time),(.72,8.6)); self.assertEqual((panels[1].start_time,panels[1].end_time),(9.66,12.52))
    def test_segment_mapping_is_one_panel_per_segment(self):
        segments=[TranscriptSegment("One",1,2),TranscriptSegment("Two",3,4)]
        panels=[Panel("one","one"),Panel("two","two"),Panel("extra","extra")]
        assign_timings(panels,[],segments)
        self.assertEqual((panels[0].start_time,panels[0].end_time),(1,2)); self.assertEqual((panels[1].start_time,panels[1].end_time),(3,4)); self.assertEqual((panels[2].start_time,panels[2].end_time),(0,0))
        project=Project(panels=panels,segments=segments,words=[Word("word",1,2)],audio_path="missing.mp3")
        self.assertTrue(any("Panel count" in message for message in validate_render(project)))
    def test_render_preserves_segment_pauses(self):
        project=Project(panels=[Panel("one","one",start_time=.72,end_time=8.6),Panel("two","two",start_time=9.66,end_time=12.52),Panel("three","three",start_time=13.18,end_time=15.22)],words=[Word("end",13.18,15.22)])
        self.assertAlmostEqual(panel_render_duration(project,0),9.66)
        self.assertAlmostEqual(panel_render_duration(project,1),3.52)
        self.assertAlmostEqual(panel_render_duration(project,2),2.04)
    def test_render_uses_default_background_music_when_present(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); image=root/"panel.jpg"; music=root/"background_music.mp3"; image.write_bytes(b"image"); music.write_bytes(b"music")
            project=Project(panels=[Panel(str(image),image.name,start_time=0,end_time=1)])
            commands=[]
            with patch("app.renderer.shutil_which",return_value="ffmpeg"), patch("app.renderer.default_background_music_path",return_value=music), patch("app.renderer._run",side_effect=lambda command,cancel=None:commands.append(command)):
                renderer.render(project,str(root/"out.mp4"),preview=True)
        final=commands[-1]
        self.assertIn("-stream_loop",final); self.assertIn(str(music),final)
        self.assertIn("-filter:a",final); self.assertIn("volume=0.160",final)
        self.assertIn("-map",final); self.assertIn("1:a:0",final)
    def test_render_uses_cropped_panel_cache(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); image=root/"panel.jpg"; cropped=root/"panel-crop.png"; image.write_bytes(b"image"); cropped.write_bytes(b"crop")
            project=Project(panels=[Panel(str(image),image.name,start_time=0,end_time=1,crop_path=str(cropped),crop_rect=(0,0,1,1))])
            commands=[]
            with patch("app.renderer.shutil_which",return_value="ffmpeg"), patch("app.renderer.default_background_music_path",return_value=None), patch("app.renderer._run",side_effect=lambda command,cancel=None:commands.append(command)):
                renderer.render(project,str(root/"out.mp4"),preview=True)
        self.assertIn(str(cropped),commands[0]); self.assertNotIn(str(image),commands[0])
    def test_parallel_panel_render_keeps_quality_settings(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); audio=root/"voice.mp3"; audio.write_bytes(b"audio")
            panels=[]
            for index in range(2):
                image=root/f"panel-{index}.png"; image.write_bytes(b"image"); panels.append(Panel(str(image),image.name,start_time=float(index),end_time=float(index+1)))
            commands=[]; project=Project(panels=panels,audio_path=str(audio),words=[Word("voice",0,2)])
            with patch("app.renderer.shutil_which",return_value="ffmpeg"), patch("app.renderer.default_background_music_path",return_value=None), patch("app.renderer._run",side_effect=lambda command,cancel=None:commands.append(command)), patch("app.renderer.os.cpu_count",return_value=8):
                renderer.render(project,str(root/"out.mp4"))
        clips=[command for command in commands if "-loop" in command]
        self.assertEqual(len(clips),2)
        self.assertTrue(all("scale=4320:7680" in command[command.index("-vf")+1] for command in clips))
        self.assertTrue(all(command[command.index("-preset")+1] == "medium" for command in clips))
        self.assertTrue(all(command[command.index("-threads")+1] == "4" for command in clips))
        final=commands[-1]
        self.assertEqual(final[final.index("-preset")+1],"slow")
        self.assertEqual(final[final.index("-crf")+1],"18")
    def test_panel_render_workers_are_bounded(self):
        self.assertEqual(renderer.panel_render_workers(1),1)
        with patch("app.renderer.os.cpu_count",return_value=8): self.assertEqual(renderer.panel_render_workers(4),2)
        with patch("app.renderer.os.cpu_count",return_value=4): self.assertEqual(renderer.panel_render_workers(4),1)

if __name__ == "__main__": unittest.main()
