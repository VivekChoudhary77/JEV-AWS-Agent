"""make_demo.py: segment validation offline, plus small real encodes with the bundled ffmpeg."""

import subprocess

import imageio_ffmpeg
import pytest
from PIL import Image

import make_demo

VIDEO = make_demo.Video(6.0, 320, 240)


def segment(**overrides):
    return {"start": 0, "end": 4, "speed": 2, **overrides}


def parsed(**overrides):
    return make_demo.parse_segment(segment(**overrides), VIDEO, "segment 1")


def write_segments(path, text):
    path.write_text(text)
    return path


@pytest.fixture(autouse=True)
def demo_dir(monkeypatch, tmp_path):
    folder = tmp_path / "jev-demo"
    monkeypatch.setattr(make_demo, "DEMO_DIR", folder)
    return folder


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    path = tmp_path_factory.mktemp("source") / "source.mp4"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=duration=6:size=320x240:rate=30", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def test_a_valid_segment_parses():
    result = parsed(caption=" Hello ", blur=[1, 2, 30, 40])
    assert (result.start, result.end, result.speed, result.caption) == (0, 4, 2, "Hello")
    assert result.blur == [[1, 2, 30, 40]]


def test_caption_and_blur_are_optional():
    result = parsed()
    assert (result.caption, result.blur) == ("", [])


def test_several_blur_boxes_are_accepted():
    assert parsed(blur=[[0, 0, 10, 10], [20, 20, 10, 10]]).blur == [[0, 0, 10, 10], [20, 20, 10, 10]]


@pytest.mark.parametrize("overrides, message", [
    ({"start": 4, "end": 4}, "start < end"),
    ({"start": 5, "end": 4}, "start < end"),
    ({"start": -1}, "start < end"),
    ({"speed": 0}, "speed must be above 0"),
    ({"speed": -2}, "speed must be above 0"),
    ({"end": 7}, "past the clip"),
    ({"start": "1"}, "start must be a number"),
    ({"speed": True}, "speed must be a number"),
    ({"speed": float("nan")}, "speed must be a number"),
    ({"end": float("inf")}, "end must be a number"),
    ({"spped": 2}, "unknown keys"),
    ({"caption": 5}, "caption must be text"),
    ({"blur": [1, 2, 3]}, "blur must be"),
    ({"blur": [1.5, 2, 3, 4]}, "blur must be"),
    ({"blur": [300, 0, 50, 10]}, "isn't inside"),
    ({"blur": [0, 0, 0, 10]}, "isn't inside"),
])
def test_bad_segments_are_refused_with_a_clear_error(overrides, message):
    with pytest.raises(SystemExit, match=message):
        parsed(**overrides)


@pytest.mark.parametrize("key", ["start", "end", "speed"])
def test_a_missing_required_key_is_named(key):
    raw = segment()
    del raw[key]
    with pytest.raises(SystemExit, match=f"missing {key}"):
        make_demo.parse_segment(raw, VIDEO, "segment 1")


def test_errors_name_the_file_and_segment(tmp_path):
    path = write_segments(tmp_path / "cuts.yaml", "- {start: 0, end: 2, speed: 1}\n- {start: 3, end: 2, speed: 1}\n")
    with pytest.raises(SystemExit, match="cuts.yaml segment 2"):
        make_demo.load_segments(path, VIDEO)


@pytest.mark.parametrize("text", ["[]", "start: 1", "just text"])
def test_a_file_that_isnt_a_list_of_segments_is_refused(tmp_path, text):
    with pytest.raises(SystemExit, match="non-empty list"):
        make_demo.load_segments(write_segments(tmp_path / "cuts.yaml", text), VIDEO)


def test_an_end_within_the_banners_rounding_is_clamped():
    assert parsed(end=6.04).end == 6.0


@pytest.mark.parametrize("video, expected", [
    (make_demo.Video(1, 1920, 1080), (1920, 1080)),
    (make_demo.Video(1, 3840, 2160), (1920, 1080)),
    (make_demo.Video(1, 1681, 963), (1680, 962)),
    (make_demo.Video(1, 2560, 1439), (1920, 1080)),
])
def test_output_is_even_and_at_most_1080p(video, expected):
    assert make_demo.output_size(video) == expected


def test_relative_outputs_land_in_the_demo_folder(demo_dir):
    assert make_demo.output_path("clips/a.mp4") == (demo_dir / "clips" / "a.mp4").resolve()
    assert (demo_dir / "clips").is_dir()


@pytest.mark.parametrize("path", ["/tmp/out.mp4", "../escaped.mp4", "clips/../../escaped.mp4"])
def test_outputs_outside_the_demo_folder_are_refused(path):
    with pytest.raises(SystemExit, match="outside"):
        make_demo.output_path(path)


def test_inputs_are_found_in_the_demo_folder_too(demo_dir):
    demo_dir.mkdir()
    (demo_dir / "a.mp4").write_bytes(b"x")
    assert make_demo.input_path("a.mp4") == (demo_dir / "a.mp4").resolve()


def test_a_missing_input_is_a_clear_error():
    with pytest.raises(SystemExit, match="not found"):
        make_demo.input_path("nothing.mp4")


def test_probe_reads_a_real_file(source):
    video = make_demo.probe(source)
    assert (video.width, video.height) == (320, 240)
    assert video.duration == pytest.approx(6, abs=0.1)


def test_render_two_segments_end_to_end(source, tmp_path, demo_dir):
    cuts = write_segments(tmp_path / "cuts.yaml", """
- {start: 0, end: 4, speed: 2, caption: "Jev fills in the form"}
- {start: 4, end: 6, speed: 1}
""")
    out = make_demo.render(str(source), str(cuts), "clip.mp4")
    assert out == (demo_dir / "clip.mp4").resolve()
    video = make_demo.probe(out)
    assert video.duration == pytest.approx(4, abs=0.15)  # 4 s / 2 + 2 s / 1
    assert (video.width, video.height) == (320, 240)
    assert "h264" in banner(out) and "yuv420p" in banner(out) and "30 fps" in banner(out)
    assert "Audio:" not in banner(out)


def banner(path):
    return subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True).stderr


def frame_at(path, seconds, out):
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-ss", str(seconds),
                    "-i", str(path), "-frames:v", "1", str(out)], check=True)
    return Image.open(out).convert("RGB")


def test_a_caption_is_burned_in_bottom_center(source, tmp_path):
    plain = write_segments(tmp_path / "plain.yaml", "- {start: 0, end: 1, speed: 1}\n")
    captioned = write_segments(tmp_path / "cap.yaml", "- {start: 0, end: 1, speed: 1, caption: Hello}\n")
    before = frame_at(make_demo.render(str(source), str(plain), "plain.mp4"), 0.5, tmp_path / "a.png")
    after = frame_at(make_demo.render(str(source), str(captioned), "cap.mp4"), 0.5, tmp_path / "b.png")
    region = (100, 180, 220, 235)
    assert before.crop(region).tobytes() != after.crop(region).tobytes()
    assert sum(after.crop((130, 195, 190, 225)).convert("L").point(lambda v: v > 225).convert("L").tobytes()) > 0


def test_a_blur_box_is_applied_only_inside_its_box(source, tmp_path):
    plain = write_segments(tmp_path / "plain.yaml", "- {start: 0, end: 1, speed: 1}\n")
    blurred = write_segments(tmp_path / "blur.yaml", "- {start: 0, end: 1, speed: 1, blur: [100, 60, 120, 120]}\n")
    before = frame_at(make_demo.render(str(source), str(plain), "plain.mp4"), 0.5, tmp_path / "a.png")
    after = frame_at(make_demo.render(str(source), str(blurred), "blur.mp4"), 0.5, tmp_path / "b.png")
    inside = (110, 70, 210, 170)
    assert before.crop(inside).tobytes() != after.crop(inside).tobytes()
    outside = (0, 0, 80, 50)
    difference = sum(abs(a - b) for a, b in zip(before.crop(outside).tobytes(), after.crop(outside).tobytes()))
    assert difference / (80 * 50 * 3) < 8


def test_a_blur_at_an_odd_position_covers_exactly_what_was_asked(source, tmp_path):
    cuts = write_segments(tmp_path / "blur.yaml", "- {start: 0, end: 1, speed: 1, blur: [101, 61, 51, 37]}\n")
    assert make_demo.probe(make_demo.render(str(source), str(cuts), "odd.mp4")).duration > 0


def test_drawtext_is_only_used_when_ffmpeg_has_it(source, tmp_path, monkeypatch):
    monkeypatch.setattr(make_demo, "has_drawtext", lambda: False)
    calls = []
    real = make_demo.caption_png
    monkeypatch.setattr(make_demo, "caption_png", lambda *args: calls.append(args) or real(*args))
    cuts = write_segments(tmp_path / "cap.yaml", "- {start: 0, end: 1, speed: 1, caption: Hello}\n")
    make_demo.render(str(source), str(cuts), "cap.mp4")
    assert len(calls) == 1


def test_a_long_caption_wraps_inside_the_frame(tmp_path):
    path = make_demo.caption_png("word " * 60, 1920, 1080, tmp_path / "caption.png")
    width, height = Image.open(path).size
    assert width <= 1920
    assert height > 150


def test_a_caption_is_large_enough_for_a_phone(tmp_path):
    width, height = Image.open(make_demo.caption_png("Hi", 1920, 1080, tmp_path / "caption.png")).size
    assert height >= 1080 * 0.09


def test_drawtext_filter_carries_the_caption_file_and_box(tmp_path):
    text = make_demo.drawtext_filter("Hello there", 1920, 1080, "C:/fonts/a.ttf", tmp_path / "caption.txt")
    assert "fontfile='C\\:/fonts/a.ttf'" in text and "box=1" in text and "x=(w-text_w)/2" in text
    assert (tmp_path / "caption.txt").read_text() == "Hello there"


def test_render_refuses_to_overwrite_its_own_input(source, tmp_path, demo_dir):
    cuts = write_segments(tmp_path / "cuts.yaml", "- {start: 0, end: 1, speed: 1}\n")
    make_demo.render(str(source), str(cuts), "clip.mp4")
    with pytest.raises(SystemExit, match="both an input and the output"):
        make_demo.render("clip.mp4", str(cuts), "clip.mp4")


def test_sheet_tiles_every_frame_and_labels_them(source):
    out = make_demo.sheet(str(source), "sheet.png", 1, 3)
    width, height = Image.open(out).size
    assert width == make_demo.TILE_WIDTH * 3
    assert height == 240 * 2  # 6 frames in 3 columns


def test_sheet_grid_grows_to_fit_the_clip(source):
    width, height = Image.open(make_demo.sheet(str(source), "tall.png", 1, 2)).size
    assert width == make_demo.TILE_WIDTH * 2
    assert height == 240 * 3


@pytest.mark.parametrize("every, cols", [(0, 5), (-1, 5), (3, 0)])
def test_sheet_refuses_bad_settings(source, every, cols):
    with pytest.raises(SystemExit, match="must be above 0"):
        make_demo.sheet(str(source), "bad.png", every, cols)


def test_combine_joins_mismatched_clips_in_order(source, tmp_path):
    cuts = write_segments(tmp_path / "cuts.yaml", "- {start: 0, end: 2, speed: 1}\n")
    first = make_demo.render(str(source), str(cuts), "first.mp4")
    wide = tmp_path / "wide.mp4"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=duration=1:size=640x200:rate=24", "-pix_fmt", "yuv444p", str(wide)], check=True)
    video = make_demo.probe(make_demo.combine("joined.mp4", [str(first), str(wide)]))
    assert video.duration == pytest.approx(3, abs=0.15)
    assert (video.width, video.height) == (320, 240)


def test_no_temp_files_are_left_behind(source, tmp_path, demo_dir):
    cuts = write_segments(tmp_path / "cuts.yaml", "- {start: 0, end: 1, speed: 1, caption: Hello}\n")
    make_demo.render(str(source), str(cuts), "clip.mp4")
    make_demo.sheet(str(source), "sheet.png", 3, 5)
    assert sorted(path.name for path in demo_dir.iterdir()) == ["clip.mp4", "sheet.png"]


def test_cli_runs_each_command(source, tmp_path, demo_dir):
    cuts = write_segments(tmp_path / "cuts.yaml", "- {start: 0, end: 1, speed: 1}\n")
    assert make_demo.main(["sheet", str(source), "s.png", "--every", "3", "--cols", "2"]) == 0
    assert make_demo.main(["render", str(source), str(cuts), "r.mp4"]) == 0
    assert make_demo.main(["combine", "c.mp4", "r.mp4"]) == 0
    assert {path.name for path in demo_dir.iterdir()} == {"s.png", "r.mp4", "c.mp4"}
