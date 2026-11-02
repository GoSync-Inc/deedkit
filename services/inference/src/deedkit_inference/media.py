"""Looking at an upload without a model: image size from the header bytes, audio duration from `ffprobe` (a child
process of its own, killed with its group on a timeout), and the 16 kHz mono WAV chunks the speech runtimes read."""

import os
import signal
import struct
import subprocess

SNIFF_BYTES = 1 << 20
JPEG_SOF = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})
TOOL_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
CHUNK_SECONDS = 600
#: ANYM-691 criterion 1: while a decode helper of a worker runs, `<scratch>/nopause` exists and the service never leaves
#: the worker's group stopped (a stopped decoder would spend its `communicate` deadline asleep).
MARKER_NAME = "nopause"


def _webp(head):
    kind = head[12:16]
    if kind == b"VP8 " and head[23:26] == b"\x9d\x01\x2a":
        return struct.unpack("<HH", head[26:30])[0] & 0x3FFF, struct.unpack("<HH", head[26:30])[1] & 0x3FFF
    if kind == b"VP8L" and head[20] == 0x2F:
        bits = int.from_bytes(head[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if kind == b"VP8X":
        return int.from_bytes(head[24:27], "little") + 1, int.from_bytes(head[27:30], "little") + 1
    return None


def _jpeg(head):
    position = 2
    while position + 9 < len(head):
        if head[position] != 0xFF:
            position += 1
            continue
        marker = head[position + 1]
        if marker == 0xFF:
            position += 1
            continue
        if marker in JPEG_SOF:
            height, width = struct.unpack(">HH", head[position + 5:position + 9])
            return width, height
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            position += 2
            continue
        position += 2 + struct.unpack(">H", head[position + 2:position + 4])[0]
    return None


def image_size(path):
    """(width, height) from the file header, or None when it is none of png, jpeg, gif, webp."""
    with open(path, "rb") as handle:
        head = handle.read(SNIFF_BYTES)
    if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
        return struct.unpack(">II", head[16:24])
    if head[:6] in (b"GIF87a", b"GIF89a") and len(head) >= 10:
        return struct.unpack("<HH", head[6:10])
    if head[:2] == b"\xff\xd8":
        return _jpeg(head)
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP" and len(head) >= 30:
        return _webp(head)
    return None


def _mark(marker):
    try:
        os.close(os.open(marker, os.O_WRONLY | os.O_CREAT, 0o600))
    except OSError:
        pass  # no marker: the yielder may then pause this phase, which only costs the helper its deadline margin


def run_tool(argv, timeout, env=None, own_group=True, marker=None):
    """Run a helper with a timeout; (returncode, stdout) or (None, b"") on a timeout. `own_group=False` keeps it in
    the caller's process group: inside a worker that is the worker's group, so killing the worker kills the helper
    and the supervisor counts its memory with the worker's. `marker` is the path of the no-pause marker that exists
    from before the helper starts until it is done."""
    if marker is not None:
        _mark(marker)
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   env=env or {"PATH": TOOL_PATH}, start_new_session=own_group)
        try:
            out, _ = process.communicate(timeout=timeout)
            return process.returncode, out
        except subprocess.TimeoutExpired:
            return None, b""
        finally:
            try:
                if own_group:
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except (ProcessLookupError, PermissionError):
                pass
            process.wait()
    finally:
        if marker is not None:
            try:
                os.unlink(marker)
            except OSError:
                pass


def probe_duration(path, ffprobe, timeout=20.0):
    """Seconds of audio, from ffprobe; ValueError when the file is not audio ffprobe can read."""
    code, out = run_tool([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of",
                          "default=noprint_wrappers=1:nokey=1", str(path)], timeout)
    try:
        value = float(out.decode("ascii", "replace").strip())
    except ValueError:
        raise ValueError("unreadable") from None
    if code != 0 or not 0 < value < 1e7:
        raise ValueError("unreadable")
    return value


def chunk_wav(ffmpeg, source, offset, length, destination, timeout, marker=None):
    """One window of `source` as 16 kHz mono 16-bit WAV. False when ffmpeg failed or produced nothing. Only workers
    call it, so ffmpeg stays in the worker's process group (see run_tool)."""
    code, _ = run_tool([ffmpeg, "-nostdin", "-v", "error", "-y", "-ss", f"{offset:.3f}", "-t", f"{length:.3f}",
                        "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav",
                        str(destination)], timeout, own_group=False, marker=marker)
    try:
        return code == 0 and os.path.getsize(destination) > 44
    except OSError:
        return False


def find_tool(name, option=None):
    """Absolute path of a helper binary: the configured one (absolute), else the first executable on TOOL_PATH."""
    if option is not None:
        return option if os.path.isabs(option) and os.access(option, os.X_OK) else None
    for directory in TOOL_PATH.split(":"):
        path = os.path.join(directory, name)
        if os.access(path, os.X_OK):
            return path
    return None


def iter_chunks(ffmpeg, source, duration, scratch, window=CHUNK_SECONDS, timeout=180.0):
    """(offset, length, wav path) for consecutive windows of `source`; the WAV is replaced for the next window, so
    at most one window of decoded audio exists at a time. RuntimeError when ffmpeg cannot decode a window."""
    path, offset, marker = os.path.join(scratch, "window.wav"), 0.0, os.path.join(scratch, MARKER_NAME)
    while offset < duration:
        length = min(float(window), duration - offset)
        if not chunk_wav(ffmpeg, source, offset, length, path, timeout, marker):
            raise RuntimeError("decode_failed")
        try:
            yield offset, length, path
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        offset += length
