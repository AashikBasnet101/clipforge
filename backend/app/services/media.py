import json
import os
import re
import subprocess
from fractions import Fraction

from ..config import MAX_DURATION
from ..schemas import Caption


class MediaError(ValueError):
    pass


def run(args, timeout=300, diagnostics=False):
    try:
        result = subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout)
        return result.stderr if diagnostics else result.stdout
    except subprocess.TimeoutExpired:
        raise MediaError("Media processing exceeded the time limit") from None
    except subprocess.CalledProcessError:
        raise MediaError("FFmpeg could not decode or encode this media") from None


def input_format(path):
    with path.open("rb") as handle:
        header = handle.read(32)
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        return "matroska", "video/webm"
    if len(header) >= 12 and header[4:8] in (b"ftyp", b"moov", b"mdat", b"wide", b"free"):
        return "mov", "video/quicktime" if header[8:12] == b"qt  " else "video/mp4"
    raise MediaError("Upload a valid MP4, MOV, or WebM video")


def probe(path, source=True):
    fmt, mime = input_format(path)
    data = json.loads(run(["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-f", fmt,
                           "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,pix_fmt,avg_frame_rate:stream_disposition=attached_pic",
                           "-of", "json", str(path)], 60))
    video = next((s for s in data.get("streams", []) if s["codec_type"] == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in data.get("streams", []) if s["codec_type"] == "audio"), None)
    duration = float(data.get("format", {}).get("duration", 0))
    if not video or not audio or not 0 < duration <= (MAX_DURATION if source else 181):
        raise MediaError("Video must contain video and audio within the configured duration limit")
    if max(video["width"], video["height"]) > 8192:
        raise MediaError("Video resolution exceeds 8192 pixels")
    return {"duration": duration, "width": video["width"], "height": video["height"], "mime": mime,
            "video_codec": video["codec_name"], "audio_codec": audio["codec_name"], "pixel_format": video.get("pix_fmt"), "frame_rate": video.get("avg_frame_rate")}


def ffmpeg(source, options, output, timeout=900, seek=None, duration=None):
    fmt, _ = input_format(source)
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-filter_threads", "2", "-threads", "2", "-protocol_whitelist", "file"]
    if seek is not None:
        args += ["-ss", str(seek)]
    args += ["-f", fmt, "-i", str(source)]
    if duration is not None:
        args += ["-t", str(duration)]
    run(args + options + [str(output)], timeout)


def extract(source, output, start, duration):
    codec = "pcm_s16le" if output.suffix == ".wav" else "flac"
    ffmpeg(source, ["-map", "0:a:0", "-ac", "1", "-ar", "16000", "-c:a", codec], output,
           seek=start, duration=duration)


def browser_compatible(meta):
    return (meta["mime"], meta["video_codec"], meta["audio_codec"], meta["pixel_format"]) == (
        "video/mp4", "h264", "aac", "yuv420p"
    )


def preview(source, output):
    ffmpeg(source, ["-map", "0:V:0", "-map", "0:a:0", "-vf",
                    "scale=640:640:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1,fps=24",
                    "-c:v", "libx264", "-threads", "2", "-preset", "ultrafast", "-crf", "27",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"], output)


def clock(seconds, ass=False):
    scale = 100 if ass else 1000
    ticks = max(0, round(seconds * scale))
    whole, fraction = divmod(ticks, scale)
    hours, remainder = divmod(whole, 3600)
    minutes, seconds = divmod(remainder, 60)
    if ass:
        return f"{hours}:{minutes:02}:{seconds:02}.{fraction:02}"
    return f"{hours:02}:{minutes:02}:{seconds:02},{fraction:03}"


def cues(segments, settings):
    if settings.captions is not None:
        return [(item, []) for item in settings.captions]
    result = []
    for segment in segments:
        start = max(segment.start, settings.start)
        end = min(segment.end, settings.end)
        if end <= start:
            continue
        words = [word for word in segment.words if word["end"] > start and word["start"] < end]
        if not words:
            result.append((Caption(start=start-settings.start, end=end-settings.start, text=segment.text), []))
        for index in range(0, len(words), settings.words_per_line):
            group = words[index:index + settings.words_per_line]
            left = max(start, group[0]["start"]) - settings.start
            right = min(end, group[-1]["end"]) - settings.start
            if right > left:
                result.append((Caption(start=left, end=right, text=" ".join(w["word"] for w in group)), group))
    return sorted(result, key=lambda item: item[0].start)


def quiet_intervals(source, settings):
    fmt, _ = input_format(source)
    log = run(["ffmpeg", "-nostdin", "-hide_banner", "-v", "info", "-ss", str(settings.start),
               "-t", str(settings.end-settings.start), "-protocol_whitelist", "file", "-f", fmt,
               "-i", str(source), "-map", "0:a:0", "-vn", "-af", "silencedetect=n=-35dB:d=0.55",
               "-f", "null", "-"], timeout=60, diagnostics=True)
    left = None
    for event, value in re.findall(r"silence_(start|end): (-?\d+(?:\.\d+)?)", log):
        if event == "start":
            left = float(value)
        elif left is not None:
            yield left, float(value)
            left = None


def edit_intervals(segments, settings, quiet):
    duration = settings.end-settings.start
    if not settings.shorten_pauses:
        return [(0, duration)]
    speech = []
    for segment in segments:
        if segment.start >= settings.end or segment.end <= settings.start:
            continue
        timestamps = [(w["start"], w["end"]) for w in segment.words] or [(segment.start, segment.end)]
        speech.extend((max(0, start-settings.start), min(duration, end-settings.start))
                      for start, end in timestamps if end > settings.start and start < settings.end)
    if len(speech) < 2:
        return [(0, duration)]
    intervals = []
    start = 0
    previous_end = 0
    for left, right in sorted(speech):
        # Preserve natural speech edges and skip tiny fragments.
        if previous_end and left-previous_end >= 1.0 and len(intervals) < 8:
            matches = [(max(q_start+.07, previous_end+.22), min(q_end-.07, left-.18))
                       for q_start, q_end in quiet]
            cut_start, cut_end = max(matches, key=lambda pair: pair[1]-pair[0], default=(0, 0))
            if cut_end-cut_start >= .55 and cut_start-start >= 1.0 and duration-cut_end >= 1.0:
                intervals.append((start, cut_start))
                start = cut_end
        previous_end = max(previous_end, right)
    return [*intervals, (start, duration)]


def edited_cues(items, intervals, source_start):
    if len(intervals) == 1:
        return items
    results = []
    offset = 0
    for left, right in intervals:
        for cue, words in items:
            start, end = max(cue.start, left), min(cue.end, right)
            if end <= start:
                continue
            included = [word for word in words if word["end"]-source_start > start
                        and word["start"]-source_start < end]
            if words and not included:
                continue
            text = " ".join(word["word"] for word in included) if included else cue.text
            cue = Caption(start=max(0, offset+start-left), end=offset+end-left, text=text)
            aligned = [{**word, "start": word["start"]+offset-left, "end": word["end"]+offset-left}
                       for word in included]
            results.append((cue, aligned))
        offset += right-left
    return results


def ass_text(value):
    return value.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\r", "").replace("\n", r"\N")


def color(value):
    return f"&H00{value[5:7]}{value[3:5]}{value[1:3]}"


def captions_files(segments, settings, width, height, folder, intervals=None):
    intervals = intervals or [(0, settings.end-settings.start)]
    items = edited_cues(cues(segments, settings), intervals, settings.start)
    duration = sum(end-start for start, end in intervals)
    srt = folder / "captions.srt"
    srt.write_text("\n".join(f"{i}\n{clock(c.start)} --> {clock(c.end)}\n{c.text}\n"
                             for i, (c, _) in enumerate(items, 1)), encoding="utf-8")
    ass = folder / "captions.ass"
    align = {"top": 8, "middle": 5, "bottom": 2}[settings.position]
    bold = -1 if settings.style in ("creator", "highlight", "karaoke") else 0
    border = 3 if settings.style == "educational" else 1
    ass.write_text(f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Captions,{settings.font},{settings.font_size},{color(settings.text_color)},{color(settings.highlight_color)},&H00141110,&H80141110,{bold},0,0,0,100,100,0,0,{border},{settings.stroke},{settings.shadow},{align},{width//9},{width//9},{height//7},1
Style: Brand,{settings.font},{settings.font_size},&H00FFFFFF,&H00000000,&H00141110,&H80141110,-1,0,0,0,100,100,0,0,1,3,1,{2 if align == 8 else 8},{width//9},{width//9},{height//10},1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
""", encoding="utf-8")
    with ass.open("a", encoding="utf-8") as handle:
        def line(start, end, style, body):
            if clock(end, True) != clock(start, True):
                handle.write(f"Dialogue: 0,{clock(start, True)},{clock(end, True)},{style},,0,0,0,,{body}\n")

        if settings.overlay.strip():
            line(0, duration, "Brand", ass_text(settings.overlay))
        if settings.style == "off":
            return ass, srt
        for cue, words in items:
            if words and settings.style in ("highlight", "karaoke"):
                if settings.style == "karaoke":
                    parts = []
                    for index, word in enumerate(words):
                        next_start = words[index+1]["start"]-settings.start if index+1 < len(words) else cue.end
                        duration = max(1, round((next_start - max(cue.start, word["start"]-settings.start))*100))
                        parts.append(r"{\kf" + str(duration) + "}" + ass_text(word["word"]))
                    prefix = r"{\1c" + color(settings.highlight_color) + r"&\2c" + color(settings.text_color) + "&}"
                    line(cue.start, cue.end, "Captions", prefix + " ".join(parts))
                else:
                    for index, word in enumerate(words):
                        start = max(cue.start, word["start"]-settings.start)
                        end = min(cue.end, words[index+1]["start"]-settings.start) if index+1 < len(words) else cue.end
                        if end <= start:
                            continue
                        parts = [ass_text(item["word"]) for item in words]
                        parts[index] = r"{\c" + color(settings.highlight_color) + "&}" + parts[index] + r"{\r}"
                        line(start, end, "Captions", " ".join(parts))
            else:
                line(cue.start, cue.end, "Captions", ass_text(cue.text))
    return ass, srt


def face_crop(source, start, end, ratio):
    if os.getenv("ENABLE_FACE_TRACKING") != "1":
        return None
    try:
        import cv2
        import statistics
        cap = cv2.VideoCapture(str(source))
        model = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        centers = []
        try:
            for index in range(9):
                cap.set(cv2.CAP_PROP_POS_MSEC, 1000*(start+(end-start)*(index+.5)/9))
                ok, frame = cap.read()
                if not ok:
                    continue
                h, w = frame.shape[:2]
                if w/h <= ratio:
                    return None
                small = cv2.resize(frame, (480, max(1, round(h*480/w))))
                faces = model.detectMultiScale(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), minSize=(35, 35))
                if len(faces) == 1:
                    x, _, face_width, _ = faces[0]
                    centers.append(float(x+face_width/2)/480)
        finally:
            cap.release()
        if len(centers) >= 6 and max(centers)-min(centers) <= .18:
            fraction = h*ratio/w
            return max(0, min(1, (statistics.median(centers)-fraction/2)/(1-fraction)))
    except (ImportError, RuntimeError):
        return None
    return None


def render(source, settings, segments, folder, progress=lambda: None):
    width, height = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080)}[settings.ratio]
    quiet = list(quiet_intervals(source, settings)) if settings.shorten_pauses else []
    intervals = edit_intervals(segments, settings, quiet)
    duration = sum(end-start for start, end in intervals)
    ass, srt = captions_files(segments, settings, width, height, folder, intervals)
    mode = settings.framing
    crop = settings.crop
    if mode == "smart":
        detected = face_crop(source, settings.start, settings.end, width/height)
        if detected is None:
            mode = "full-frame fit"
        else:
            crop, mode = detected, "stable single-face framing"
    if mode in ("fit", "full-frame fit"):
        frame = f"scale={width}:{height}:flags=lanczos:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x101214"
    else:
        frame = f"scale={width}:{height}:flags=lanczos:force_original_aspect_ratio=increase:force_divisible_by=2,crop={width}:{height}:(iw-ow)*{crop}:(ih-oh)*{crop}"
    # TemporaryDirectory produces an application-controlled absolute path.
    filter_path = str(ass).replace("\\", r"\\").replace(":", r"\:").replace("'", r"\'")
    chain = f"{frame},setsar=1"
    source_rate = settings.fps
    if not source_rate:
        try:
            source_rate = min(Fraction(probe(source)["frame_rate"]), 120)
        except (ValueError, ZeroDivisionError, TypeError):
            source_rate = 30
    chain += f",fps={source_rate or 30}"
    if settings.style != "off" or settings.overlay:
        chain += f",ass=filename='{filter_path}'"
    output = folder / "clip.mp4"
    audio = "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000" if settings.normalize_audio else "anull"
    if len(intervals) == 1:
        args = ["-map", "0:V:0", "-map", "0:a:0", "-vf", chain, "-af", audio]
    else:
        count = len(intervals)
        graph = [f"[0:V:0]split={count}" + "".join(f"[vi{i}]" for i in range(count)),
                 f"[0:a:0]asplit={count}" + "".join(f"[ai{i}]" for i in range(count))]
        streams = []
        for i, (start, end) in enumerate(intervals):
            graph.extend([f"[vi{i}]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS[v{i}]",
                          f"[ai{i}]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[a{i}]"])
            streams.extend([f"[v{i}]", f"[a{i}]"])
        graph.append("".join(streams) + f"concat=n={count}:v=1:a=1[v][a]")
        graph.extend([f"[v]{chain}[video]", f"[a]{audio}[audio]"])
        args = ["-filter_complex_threads", "2", "-filter_complex", ";".join(graph),
                "-map", "[video]", "-map", "[audio]"]
    high = settings.quality == "high"
    args += ["-pix_fmt", "yuv420p", "-c:v", "libx264", "-threads", "4",
             "-preset", "fast" if high else "veryfast", "-crf", "16" if high else "22",
             "-c:a", "aac", "-b:a", "256k" if high else "160k", "-movflags", "+faststart",
             "-map_metadata", "-1", "-metadata", f"title={settings.title}", "-metadata", f"comment={settings.hashtags}"]
    ffmpeg(source, args, output, seek=settings.start, duration=duration)
    progress()
    meta = probe(output, source=False)
    if ((meta["width"], meta["height"], meta["video_codec"], meta["audio_codec"]) != (width, height, "h264", "aac")
            or abs(meta["duration"]-duration) > .45):
        raise MediaError("Rendered output failed format or duration validation")
    if len(intervals) > 1:
        cuts = len(intervals)-1
        mode = f"{mode} · {cuts} {'pause' if cuts == 1 else 'pauses'} shortened"
    return output, srt, mode
