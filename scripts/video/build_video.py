"""Turn the recorded frames + scenes.json into a narrated MP4.

Per scene: narration is synthesised (edge-tts, Persian neural voice); the scene's footage is fitted to the narration —
long waits (e.g. the build) are fast-forwarded (up to MAX_SPEED×), short actions are held on their last frame.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import wave
from pathlib import Path

import edge_tts
import imageio_ffmpeg

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(os.getenv("VIDEO_OUT", ROOT / "data" / "video"))
FINAL = Path(os.getenv("VIDEO_FILE", ROOT / "demo_video" / "sokan-demo.mp4"))
VOICE = os.getenv("VOICE", "fa-IR-FaridNeural")
RATE = os.getenv("VOICE_RATE", "+6%")
MAX_SPEED = float(os.getenv("MAX_SPEED", "5"))
PAD = 0.8  # silence after each narration
SR = 24000
FF = imageio_ffmpeg.get_ffmpeg_exe()


def ff(*args: str) -> None:
    subprocess.run([FF, "-y", "-loglevel", "error", *args], check=True)


async def tts(text: str, path: Path) -> None:
    if path.exists() and path.stat().st_size > 1000:
        return
    for attempt in range(4):
        try:
            await edge_tts.Communicate(text, VOICE, rate=RATE).save(str(path))
            return
        except Exception as e:  # noqa: BLE001 - the free endpoint is flaky under load
            path.unlink(missing_ok=True)
            print("tts retry", path.name, type(e).__name__)
            await asyncio.sleep(2 + 3 * attempt)
    raise RuntimeError(f"TTS failed for {path.name}")


def to_pcm(mp3: Path) -> bytes:
    wav = mp3.with_suffix(".wav")
    ff("-i", str(mp3), "-ac", "1", "-ar", str(SR), "-sample_fmt", "s16", str(wav))
    with wave.open(str(wav)) as w:
        return w.readframes(w.getnframes())


def main() -> None:
    data = json.loads((OUT / "scenes.json").read_text(encoding="utf-8"))
    frames: list[tuple[float, str]] = [tuple(f) for f in data["frames"]]
    scenes = data["scenes"]
    audio_dir = OUT / "audio"
    audio_dir.mkdir(exist_ok=True)

    async def synth():
        for i, s in enumerate(scenes):
            await tts(s["narration"], audio_dir / f"{i:02d}-{s['id']}.mp3")
    asyncio.run(synth())

    pcm = bytearray()
    lines: list[str] = []
    total = 0.0
    last_frame = frames[0][1]
    for i, s in enumerate(scenes):
        voice = to_pcm(audio_dir / f"{i:02d}-{s['id']}.mp3")
        n = len(voice) / 2 / SR
        t0, t1 = s["t0"], s["t1"]
        d = max(0.1, t1 - t0)
        target = max(n + PAD, d / MAX_SPEED)
        speed = max(1.0, d / target)
        # footage: frame visible at t0 (last before it) + frames inside the scene
        inside = [(t, f) for t, f in frames if t0 <= t < t1]
        before = [f for t, f in frames if t < t0]
        seq = [(t0, before[-1] if before else (inside[0][1] if inside else last_frame))] + inside
        used = 0.0
        for k, (t, f) in enumerate(seq):
            nxt = seq[k + 1][0] if k + 1 < len(seq) else t1
            dur = max(0.0, (nxt - t) / speed)
            if k + 1 == len(seq):
                dur = max(dur, target - used)  # hold the last frame until narration ends
            if dur <= 0:
                continue
            lines.append(f"file '{(OUT / 'frames' / f).as_posix()}'\nduration {dur:.4f}")
            used += dur
            last_frame = f
        seg = used
        pcm += voice + b"\x00\x00" * int(round((seg - n) * SR))
        print(f"{s['id']:<14} footage {d:6.1f}s  voice {n:5.1f}s  → {seg:5.1f}s  (×{speed:.1f})")
        total += seg
    lines.append(f"file '{(OUT / 'frames' / last_frame).as_posix()}'")
    (OUT / "frames.txt").write_text("\n".join(lines), encoding="utf-8")
    with wave.open(str(OUT / "narration.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(bytes(pcm))
    print(f"total {total / 60:.1f} min")

    FINAL.parent.mkdir(parents=True, exist_ok=True)
    ff("-f", "concat", "-safe", "0", "-i", str(OUT / "frames.txt"), "-i", str(OUT / "narration.wav"),
       "-vf", "fps=30,scale=1920:1080:flags=lanczos,format=yuv420p", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
       "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-shortest", str(FINAL))
    print("wrote", FINAL, f"{FINAL.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
