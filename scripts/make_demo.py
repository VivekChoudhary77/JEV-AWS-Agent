"""Edit screen recordings of the agent into short demo clips. A dev tool, not part of the agent.

Every output and temp file goes under ~/Videos/jev-demo/, so a relative OUT lands there and an
absolute OUT anywhere else is refused. Inputs are looked up as given, then under that folder.
"""

from __future__ import annotations

import argparse
import math
import re
import subprocess
import tempfile
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import imageio_ffmpeg
import yaml
from PIL import Image, ImageDraw, ImageFont

DEMO_DIR = Path.home() / "Videos" / "jev-demo"
MAX_HEIGHT = 1080
FPS = 30
ENCODE = ["-an", "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-pix_fmt", "yuv420p",
          "-movflags", "+faststart"]
SEGMENT_KEYS = {"start", "end", "speed", "caption", "blur"}
TILE_WIDTH = 320
LABEL_SIZE = 22
CAPTION_FONT_FILES = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                      "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
                      "/System/Library/Fonts/Supplemental/Arial Bold.ttf"]


@dataclass
class Video:
    duration: float
    width: int
    height: int


@dataclass
class Segment:
    start: float
    end: float
    speed: float
    caption: str = ""
    blur: list[list[int]] = field(default_factory=list)


# Paths


def output_path(path: str | Path) -> Path:
    out = (DEMO_DIR / Path(path).expanduser()).resolve()
    if not out.is_relative_to(DEMO_DIR.resolve()):
        raise SystemExit(f"{out} is outside {DEMO_DIR}; outputs never go anywhere else")
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def input_path(path: str | Path) -> Path:
    given = Path(path).expanduser()
    for candidate in (given, DEMO_DIR / given):
        if candidate.is_file():
            return candidate.resolve()
    raise SystemExit(f"{path} not found, here or under {DEMO_DIR}")


def temp_dir() -> tempfile.TemporaryDirectory:
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=DEMO_DIR, prefix="tmp-")


def refuse_overwriting(out: Path, *sources: Path) -> None:
    if out in sources:
        raise SystemExit(f"{out} is both an input and the output")


# ffmpeg


def ffmpeg(*args: str | Path) -> None:
    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *map(str, args)]
    if (code := subprocess.run(command).returncode) != 0:
        raise SystemExit(f"ffmpeg failed with exit code {code}; its error is above")


def probe(path: Path) -> Video:
    """Reads ffmpeg's banner, because imageio-ffmpeg bundles no ffprobe."""
    banner = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                            capture_output=True, text=True).stderr
    size = re.search(r"Video:.*?, (\d{2,5})x(\d{2,5})", banner)
    if not size:
        raise SystemExit(f"{path} has no video stream")
    length = re.search(r"Duration: (\d+):(\d\d):(\d\d\.\d+)", banner)
    if length:
        hours, minutes, seconds = length.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    else:  # some recorders write no duration, so count the frames instead
        duration = imageio_ffmpeg.count_frames_and_secs(str(path))[1]
    return Video(duration, int(size[1]), int(size[2]))


def has_drawtext() -> bool:
    return "drawtext" in subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-filters"],
                                        capture_output=True, text=True).stdout


def output_size(video: Video) -> tuple[int, int]:
    height = min(video.height, MAX_HEIGHT)
    width = video.width * height / video.height
    return int(width) // 2 * 2, height // 2 * 2


# Segments


def is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def blur_boxes(raw: object, video: Video, where: str) -> list[list[int]]:
    boxes = raw if isinstance(raw, list) and raw and isinstance(raw[0], list) else [raw]
    for box in boxes:
        if not (isinstance(box, list) and len(box) == 4 and all(isinstance(n, int) and not isinstance(n, bool) for n in box)):
            raise SystemExit(f"{where}: blur must be [x, y, w, h] in whole source pixels, got {box!r}")
        x, y, w, h = box
        if not (x >= 0 and y >= 0 and w > 0 and h > 0 and x + w <= video.width and y + h <= video.height):
            raise SystemExit(f"{where}: blur {box} isn't inside the {video.width}x{video.height} frame")
    return boxes


def parse_segment(raw: object, video: Video, where: str) -> Segment:
    if not isinstance(raw, dict):
        raise SystemExit(f"{where}: expected start, end and speed, got {raw!r}")
    if unknown := set(raw) - SEGMENT_KEYS:
        raise SystemExit(f"{where}: unknown keys {sorted(unknown)}; allowed: {sorted(SEGMENT_KEYS)}")
    for key in ("start", "end", "speed"):
        if key not in raw:
            raise SystemExit(f"{where}: missing {key}")
        if not is_number(raw[key]):
            raise SystemExit(f"{where}: {key} must be a number, got {raw[key]!r}")
    start, end, speed = raw["start"], raw["end"], raw["speed"]
    if not 0 <= start < end:
        raise SystemExit(f"{where}: need 0 <= start < end, got start={start}, end={end}")
    if end > video.duration + 0.05:  # the banner rounds to 10 ms
        raise SystemExit(f"{where}: end={end} is past the clip's {video.duration:.2f} s")
    if speed <= 0:
        raise SystemExit(f"{where}: speed must be above 0, got {speed}")
    caption = raw.get("caption", "")
    if not isinstance(caption, str):
        raise SystemExit(f"{where}: caption must be text, got {caption!r}")
    return Segment(start, min(end, video.duration), speed, caption.strip(),
                   blur_boxes(raw["blur"], video, where) if "blur" in raw else [])


def load_segments(path: Path, video: Video) -> list[Segment]:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, list) or not raw:
        raise SystemExit(f"{path}: expected a non-empty list of segments")
    return [parse_segment(item, video, f"{path.name} segment {number}") for number, item in enumerate(raw, 1)]


# Captions


def wrap(text: str, font: ImageFont.ImageFont, max_width: float) -> list[str]:
    lines = []
    for paragraph in text.splitlines():
        line = ""
        for word in paragraph.split():
            trial = f"{line} {word}".strip()
            if line and font.getlength(trial) > max_width:
                lines.append(line)
                line = word
            else:
                line = trial
        lines.append(line)
    return lines


def caption_png(text: str, width: int, height: int, path: Path) -> Path:
    """White text on a dark box. Sized from the output height so that it stays readable on a phone."""
    size = max(28, round(height * 0.055))
    padding = size // 2
    font = ImageFont.load_default(size=size)
    lines = wrap(text, font, width * 0.9 - 2 * padding)
    line_height = round(size * 1.25)
    box_width = min(width, round(max(map(font.getlength, lines))) + 2 * padding)
    image = Image.new("RGBA", (box_width, line_height * len(lines) + 2 * padding), (0, 0, 0, 165))
    draw = ImageDraw.Draw(image)
    for number, line in enumerate(lines):
        draw.text((box_width / 2, padding + line_height * (number + 0.5)), line, font=font,
                  fill=(255, 255, 255, 255), anchor="mm")
    image.save(path)
    return path


def caption_font_file() -> str | None:
    return next((font for font in CAPTION_FONT_FILES if Path(font).is_file()), None)


def filter_path(path: str | Path) -> str:
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def drawtext_filter(text: str, width: int, height: int, font_file: str, path: Path) -> str:
    size = max(28, round(height * 0.055))
    path.write_text(textwrap.fill(text, max(10, int(width * 0.9 / (size * 0.6)))))
    margin = round(height * 0.05)
    return (f"drawtext=fontfile='{filter_path(font_file)}':textfile='{filter_path(path)}':fontsize={size}:"
            f"fontcolor=white:box=1:boxcolor=black@0.65:boxborderw={size // 2}:line_spacing={size // 4}:"
            f"x=(w-text_w)/2:y=h-text_h-{margin}")


# Commands


def sheet(source: str, out: str, every: float, cols: int) -> Path:
    if every <= 0 or cols <= 0:
        raise SystemExit("--every and --cols must be above 0")
    path = input_path(source)
    target = output_path(out)
    with temp_dir() as tmp:
        ffmpeg("-i", path, "-vf", f"fps=1/{every}:start_time=0,scale={TILE_WIDTH}:-2", "-fps_mode", "vfr",
               Path(tmp) / "frame_%05d.png")
        tiles = [Image.open(frame).convert("RGB") for frame in sorted(Path(tmp).glob("frame_*.png"))]
    if not tiles:
        raise SystemExit(f"{path} produced no frames")
    width, height = tiles[0].size
    canvas = Image.new("RGB", (width * cols, height * math.ceil(len(tiles) / cols)), "black")
    font = ImageFont.load_default(size=LABEL_SIZE)
    draw = ImageDraw.Draw(canvas)
    for number, tile in enumerate(tiles):
        x, y = number % cols * width, number // cols * height
        canvas.paste(tile, (x, y))
        seconds = int(number * every)
        label = f"{seconds // 60}:{seconds % 60:02d}"
        left, top, right, bottom = draw.textbbox((x + 8, y + 6), label, font=font)
        draw.rectangle((left - 5, top - 4, right + 5, bottom + 4), fill="black")
        draw.text((x + 8, y + 6), label, font=font, fill="white")
    canvas.save(target)
    print(f"Wrote {target}: {len(tiles)} frames, {cols} columns")
    return target


def render(source: str, segments_file: str, out: str) -> Path:
    path = input_path(source)
    target = output_path(out)
    refuse_overwriting(target, path)
    video = probe(path)
    segments = load_segments(input_path(segments_file), video)
    width, height = output_size(video)
    font_file = caption_font_file() if any(s.caption for s in segments) and has_drawtext() else None
    # One input per segment: seeking is fast, and a split into concat would hold raw frames in memory.
    inputs: list[str | Path] = []
    for segment in segments:
        inputs += ["-ss", f"{segment.start:.3f}", "-t", f"{segment.end - segment.start:.3f}", "-i", path]
    with temp_dir() as tmp:
        steps, outputs, extra = [], [], len(segments)
        for number, segment in enumerate(segments):
            current = f"s{number}"
            steps.append(f"[{number}:v]setpts=(PTS-STARTPTS)/{segment.speed},format=yuv444p[{current}]")
            for box_number, (x, y, w, h) in enumerate(segment.blur):  # before scaling: boxes are source pixels
                base, patch, blurred, next_label = (f"{part}{number}_{box_number}" for part in ("b", "p", "c", "n"))
                steps.append(f"[{current}]split[{base}][{patch}]")
                steps.append(f"[{patch}]crop={w}:{h}:{x}:{y}:exact=1,boxblur={max(1, min(20, min(w, h) // 2))}:3[{blurred}]")
                steps.append(f"[{base}][{blurred}]overlay={x}:{y}[{next_label}]")
                current = next_label
            scaled = f"v{number}"
            steps.append(f"[{current}]scale={width}:{height},setsar=1[{scaled}]")
            current = scaled
            if segment.caption and font_file:
                text_file = Path(tmp) / f"caption_{number}.txt"
                steps.append(f"[{current}]{drawtext_filter(segment.caption, width, height, font_file, text_file)}[t{number}]")
                current = f"t{number}"
            elif segment.caption:
                inputs += ["-i", caption_png(segment.caption, width, height, Path(tmp) / f"caption_{number}.png")]
                margin = round(height * 0.05)
                steps.append(f"[{current}][{extra}:v]overlay=x=(W-w)/2:y=H-h-{margin}[t{number}]")
                current, extra = f"t{number}", extra + 1
            outputs.append(f"[{current}]")
        steps.append(f"{''.join(outputs)}concat=n={len(outputs)}:v=1:a=0,fps={FPS},format=yuv420p[out]")
        ffmpeg(*inputs, "-filter_complex", ";".join(steps), "-map", "[out]", *ENCODE, target)
    print(f"Wrote {target}: {len(segments)} segments, {width}x{height}")
    return target


def combine(out: str, sources: list[str]) -> Path:
    paths = [input_path(source) for source in sources]
    target = output_path(out)
    refuse_overwriting(target, *paths)
    width, height = output_size(probe(paths[0]))
    fit = (f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
           f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS},format=yuv420p")
    steps = [f"[{number}:v]{fit}[v{number}]" for number in range(len(paths))]
    steps.append("".join(f"[v{number}]" for number in range(len(paths))) + f"concat=n={len(paths)}:v=1:a=0[out]")
    inputs = [part for path in paths for part in ("-i", path)]
    ffmpeg(*inputs, "-filter_complex", ";".join(steps), "-map", "[out]", *ENCODE, target)
    print(f"Wrote {target}: {len(paths)} clips, {width}x{height}")
    return target


EXAMPLES = """\
examples:
  Pick cut points from a contact sheet, one frame every 3 seconds:
    uv run python scripts/make_demo.py sheet ~/Videos/run.mp4 run_sheet.png --every 3 --cols 5

  Cut, speed up and caption (segments.yaml):
    - {start: 0, end: 12, speed: 4, caption: "Jev fills in the form"}
    - {start: 12, end: 18, speed: 1, caption: "A human approves", blur: [1400, 0, 500, 60]}

    uv run python scripts/make_demo.py render ~/Videos/run.mp4 segments.yaml lambda.mp4

  Join finished clips:
    uv run python scripts/make_demo.py combine demo.mp4 lambda.mp4 ec2.mp4

relative outputs land in ~/Videos/jev-demo/; absolute outputs elsewhere are refused.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Edit screen recordings into short demo clips.", epilog=EXAMPLES,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    contact = commands.add_parser("sheet", help="tile timestamped frames into one image")
    contact.add_argument("input")
    contact.add_argument("out", metavar="OUT.png")
    contact.add_argument("--every", type=float, default=3, help="seconds between frames (default 3)")
    contact.add_argument("--cols", type=int, default=5, help="columns in the grid (default 5)")
    cut = commands.add_parser("render", help="cut, speed up, caption and blur segments into one clip")
    cut.add_argument("input")
    cut.add_argument("segments", metavar="SEGMENTS.yaml")
    cut.add_argument("out", metavar="OUT.mp4")
    join = commands.add_parser("combine", help="join rendered clips in the given order")
    join.add_argument("out", metavar="OUT.mp4")
    join.add_argument("clips", metavar="IN.mp4", nargs="+")
    args = parser.parse_args(argv)
    if args.command == "sheet":
        sheet(args.input, args.out, args.every, args.cols)
    elif args.command == "render":
        render(args.input, args.segments, args.out)
    else:
        combine(args.out, args.clips)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
