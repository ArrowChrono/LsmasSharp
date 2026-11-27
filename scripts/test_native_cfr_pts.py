#!/usr/bin/env python3
"""Regression for native CFR PTS rounding (requires FFmpeg and a built DLL/SO)."""
import argparse
import ctypes as C
from fractions import Fraction
from pathlib import Path
import subprocess
import tempfile

I = C.c_int32
P = C.c_void_p

class Options(C.Structure):
    _fields_ = [(n, I) for n in ("stream_index", "threads", "seek_mode", "seek_threshold",
        "direct_rendering", "fpsnum", "fpsden", "variable_info")]
    _fields_ += [("output_format", C.c_char_p), ("decoder", C.c_char_p), ("prefer_hw", I),
        ("ff_loglevel", I), ("cache_index", I), ("cachefile", C.c_char_p), ("cachedir", C.c_char_p)]
    _fields_ += [(n, I) for n in ("soft_reset", "framelist", "repeat", "dominance")]

class Info(C.Structure):
    _fields_ = [(n, I) for n in ("width", "height", "num_frames", "fps_num", "fps_den")]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--native-library", type=Path, required=True)
    args = parser.parse_args()
    lib = C.CDLL(str(args.native_library.resolve()))
    def bind(name, result, *arguments):
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = result, arguments
        return fn
    errp = C.POINTER(P)
    free = bind("lsmas_free", None, P)
    open_video = bind("lsmas_video_open_utf8", P, C.c_char_p, C.POINTER(Options), errp)
    close_video = bind("lsmas_video_close", None, P)
    get_info = bind("lsmas_video_get_info", C.c_int, P, C.POINTER(Info), errp)
    get_tb = bind("lsmas_video_get_time_base", C.c_int, P, C.POINTER(I), C.POINTER(I), errp)
    get_pts = bind("lsmas_video_get_frame_pts", C.c_int64, P, I, errp)
    def call(fn, *arguments):
        error = P()
        result = fn(*arguments, C.byref(error))
        if error:
            message = C.string_at(error).decode("utf-8", errors="replace")
            free(error)
            raise RuntimeError(message)
        return result
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "cfr.mkv"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "testsrc2=size=64x64:rate=25", "-frames:v", "150", "-c:v",
            "ffv1", "-threads", "1", str(path)], check=True)
        for num, den in [(30000, 1001), (24000, 1001), (60, 1)]:
            options = Options(stream_index=-1, threads=1, seek_threshold=10,
                fpsnum=num, fpsden=den, ff_loglevel=16, repeat=0)
            h = call(open_video, str(path).encode("utf-8"), C.byref(options))
            if not h:
                raise RuntimeError("open returned NULL")
            try:
                info = Info()
                assert call(get_info, h, C.byref(info)) == 0
                assert (info.fps_num, info.fps_den) == (num, den)
                a, b = I(), I()
                assert call(get_tb, h, C.byref(a), C.byref(b)) == 0
                assert (a.value, b.value) == (1, 1000), "fixture must use coarse MKV time base"
                assert info.num_frames > 95
                base = call(get_pts, h, 0)
                for i in range(info.num_frames):
                    x = Fraction(i * den * b.value, num * a.value)
                    expected = base + (2 * x.numerator + x.denominator) // (2 * x.denominator)
                    actual = call(get_pts, h, i)
                    assert actual == expected, (num, den, i, actual, expected)
                print(f"PASS {num}/{den}: {info.num_frames} timestamps")
            finally:
                close_video(h)

if __name__ == "__main__":
    main()
