"""Exercise native audio reads with generated media and a bounded child process."""

import argparse
import ctypes as c
import pathlib
import json
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from contextlib import contextmanager
from fractions import Fraction


class Options(c.Structure):
    _fields_ = [
        ("stream_index", c.c_int32), ("threads", c.c_int32),
        ("av_sync", c.c_int32), ("ff_loglevel", c.c_int32),
        ("decoder", c.c_char_p), ("cache_index", c.c_int32),
        ("cachefile", c.c_char_p), ("cachedir", c.c_char_p),
        ("channel_layout", c.c_uint64), ("sample_rate", c.c_int32),
        ("sample_format", c.c_int32),
    ]


class AudioInfo(c.Structure):
    _fields_ = [
        ("stream_index", c.c_int32), ("sample_rate", c.c_int32),
        ("channels", c.c_int32), ("channel_layout", c.c_uint64),
        ("sample_format", c.c_int32), ("bits_per_sample", c.c_int32),
        ("bytes_per_sample", c.c_int32), ("block_align", c.c_int32),
        ("decoded_samples", c.c_int64), ("delay_samples", c.c_int64),
        ("total_samples", c.c_int64),
    ]


class NativeAudioReadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.native = c.CDLL(str(ARGS.native_library.resolve()))
        cls.native.lsmas_audio_open_utf8.argtypes = [c.c_char_p, c.POINTER(Options), c.POINTER(c.c_void_p)]
        cls.native.lsmas_audio_open_utf8.restype = c.c_void_p
        cls.native.lsmas_audio_get_info.argtypes = [c.c_void_p, c.POINTER(AudioInfo), c.POINTER(c.c_void_p)]
        cls.native.lsmas_audio_get_info.restype = c.c_int
        cls.native.lsmas_audio_get_samples.argtypes = [c.c_void_p, c.c_void_p, c.c_int64, c.c_int64, c.POINTER(c.c_void_p)]
        cls.native.lsmas_audio_get_samples.restype = c.c_int64
        cls.native.lsmas_audio_close.argtypes = [c.c_void_p]
        cls.native.lsmas_free.argtypes = [c.c_void_p]
        cls.temp = tempfile.TemporaryDirectory(prefix="native-audio-", dir=ARGS.artifacts_dir)
        cls.directory = pathlib.Path(cls.temp.name)
        cls.source = cls.directory / "source.wav"
        cls.frames = 48000 * 90
        with wave.open(str(cls.source), "wb") as media:
            media.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
            chunk = struct.pack("<1024h", *[cls.sample(i) for i in range(1024)])
            media.writeframes((chunk * ((cls.frames + 1023) // 1024))[:cls.frames * 2])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @staticmethod
    def sample(index):
        return 1000 + index % 1024

    def error_text(self, error):
        if not error:
            return None
        text = c.string_at(error).decode("utf-8")
        self.native.lsmas_free(error)
        return text

    @contextmanager
    def opened(self, path, av_sync=1, sample_rate=0, expected_format=(48000, 1, 2)):
        options = Options(stream_index=-1, threads=1, av_sync=av_sync,
                          ff_loglevel=-8, cache_index=0, sample_format=1, sample_rate=sample_rate)
        error = c.c_void_p()
        handle = self.native.lsmas_audio_open_utf8(str(path.resolve()).encode("utf-8"), c.byref(options), c.byref(error))
        self.assertTrue(handle, self.error_text(error))
        try:
            info = AudioInfo()
            result = self.native.lsmas_audio_get_info(handle, c.byref(info), c.byref(error))
            self.assertEqual(0, result, self.error_text(error))
            self.assertEqual(expected_format, (info.sample_rate, info.channels, info.bytes_per_sample))
            yield handle, info
        finally:
            self.native.lsmas_audio_close(handle)

    def read(self, handle, start, count, channels=1):
        buffer = (c.c_int16 * max(1, count * channels))(*([-23456] * max(1, count * channels)))
        error = c.c_void_p()
        result = self.native.lsmas_audio_get_samples(handle, buffer, start, count, c.byref(error))
        return result, list(buffer)[:count * channels], self.error_text(error)

    def assert_read(self, handle, info, start, count):
        result, actual, error = self.read(handle, start, count)
        expected = [self.sample(i - info.delay_samples)
                    if 0 <= i < info.total_samples and i >= info.delay_samples else 0
                    for i in range(start, start + count)]
        self.assertEqual(count, result, error)
        self.assertIsNone(error)
        self.assertEqual(expected, actual)

    def test_api_version_metadata_matches_the_exported_abi(self):
        version = self.native.lsmas_get_api_version()
        self.assertEqual(0x010100, version)
        self.native.lsmas_get_versions_json_utf8.argtypes = [c.POINTER(c.c_void_p)]
        self.native.lsmas_get_versions_json_utf8.restype = c.c_void_p
        error = c.c_void_p()
        value = self.native.lsmas_get_versions_json_utf8(c.byref(error))
        self.assertTrue(value, self.error_text(error))
        try:
            info = json.loads(c.string_at(value))["lsmasnative"]
            self.assertEqual(version, info["api_version"])
            self.assertEqual(f"{version >> 16}.{(version >> 8) & 255}.{version & 255}", info["api_version_string"])
        finally:
            self.native.lsmas_free(value)

    def test_random_and_sequential_reads_match_source(self):
        with self.opened(self.source) as (handle, info):
            self.assertEqual(self.frames, info.total_samples)
            self.assertEqual(0, info.delay_samples)
            for start, count in [(0, 0), (0, 1), (1, 1023), (1024, 4096),
                                 (40 * 48000, 8192), (48000, 4096), (48000 + 4096, 4096)]:
                with self.subTest(start=start, count=count):
                    self.assert_read(handle, info, start, count)

    def test_timeline_boundary_padding_remains_successful(self):
        with self.opened(self.source) as (handle, info):
            for start, count in [(-32, 16), (-16, 32), (info.total_samples - 16, 32),
                                 (info.total_samples, 32), (info.total_samples + 1, 32), (0, 32)]:
                with self.subTest(start=start):
                    self.assert_read(handle, info, start, count)

    def make_delayed_media(self, name, video_delay, audio_delay):
        output = self.directory / name
        subprocess.run([
            ARGS.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-itsoffset", str(video_delay), "-f", "lavfi", "-i", "color=s=16x16:r=25:d=2",
            "-itsoffset", str(audio_delay), "-i", str(self.source),
            "-map", "0:v", "-map", "1:a", "-c:v", "ffv1", "-c:a", "pcm_s16le",
            "-t", "2", str(output),
        ], check=True, timeout=30)
        return output

    def test_positive_delay_padding_and_crossing_reads(self):
        media = self.make_delayed_media("positive-delay.mkv", 0, 0.24)
        with self.opened(media) as (handle, info):
            self.assertEqual(11520, info.delay_samples)
            self.assertEqual(info.decoded_samples + info.delay_samples, info.total_samples)
            for start, count in [(0, 32), (info.delay_samples - 32, 32),
                                 (info.delay_samples - 16, 64), (info.delay_samples + 48, 32),
                                 (info.total_samples - 16, 32)]:
                with self.subTest(start=start):
                    self.assert_read(handle, info, start, count)
        with self.opened(media, av_sync=0) as (handle, info):
            self.assertEqual(0, info.delay_samples)
            self.assert_read(handle, info, 0, 64)

    def test_negative_delay_trims_source_without_short_reads(self):
        media = self.make_delayed_media("negative-delay.mkv", 0.24, 0)
        with self.opened(media) as (handle, info):
            self.assertEqual(-11520, info.delay_samples)
            self.assertEqual(info.decoded_samples + info.delay_samples, info.total_samples)
            for start, count in [(-16, 32), (0, 64), (64, 32), (info.total_samples - 16, 32)]:
                with self.subTest(start=start):
                    self.assert_read(handle, info, start, count)

    def test_valid_silence_is_successful(self):
        media_path = self.directory / "silence.wav"
        with wave.open(str(media_path), "wb") as media:
            media.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
            media.writeframes(bytes(48000 * 2))
        with self.opened(media_path) as (handle, _):
            result, actual, error = self.read(handle, 16000, 4096)
            self.assertEqual(4096, result, error)
            self.assertIsNone(error)
            self.assertEqual([0] * 4096, actual)

    def test_truncated_in_range_read_fails_on_each_attempt(self):
        media = self.directory / "truncated.wav"
        shutil.copyfile(self.source, media)
        with self.opened(media) as (handle, info):
            self.assert_read(handle, info, 0, 1024)
            with media.open("r+b") as stream:
                stream.truncate(44 + 2048)
            for attempt in range(2):
                with self.subTest(attempt=attempt):
                    result, _, error = self.read(handle, 40 * 48000, 4096)
                    self.assertEqual(-1, result)
                    self.assertTrue(error, "A failed native read must explain the failure")

    def test_partial_read_with_leading_padding_fails_whole_request(self):
        media = self.directory / "partial.wav"
        shutil.copyfile(self.source, media)
        with self.opened(media) as (handle, info):
            with media.open("r+b") as stream:
                stream.truncate(44 + 2048)
            result, _, error = self.read(handle, -128, info.total_samples + 256)
            self.assertEqual(-1, result)
            self.assertTrue(error, "Leading padding and decoded prefixes must not hide a short read")

    def test_full_retry_recovers_after_source_is_restored(self):
        media = self.directory / "restored.wav"
        shutil.copyfile(self.source, media)
        with self.opened(media) as (handle, info):
            with media.open("r+b") as stream:
                stream.truncate(44 + 2048)
            start = 40 * 48000
            result, _, error = self.read(handle, start, 4096)
            self.assertEqual(-1, result)
            self.assertTrue(error)
            with self.source.open("rb") as source, media.open("r+b") as destination:
                shutil.copyfileobj(source, destination)
            self.assert_read(handle, info, start, 4096)
            self.assert_read(handle, info, start + 4096, 4096)

    def assert_samples_equal(self, expected, actual):
        self.assertEqual(len(expected), len(actual))
        mismatch = next((i for i, (left, right) in enumerate(zip(expected, actual)) if left != right), None)
        if mismatch is not None:
            self.fail(f"PCM sample {mismatch}: expected {expected[mismatch]}, got {actual[mismatch]}")

    def generated_codec_media(self, codec, extension, duration):
        path = self.directory / f"{codec}-{duration}.{extension}"
        subprocess.run([
            ARGS.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency=997:sample_rate=44100:duration={duration}",
            "-ac", "2", "-c:a", codec,
            *(["-aac_pns", "0"] if codec == "aac" else []), str(path),
        ], check=True, timeout=10)
        return path

    def audio_stream(self, media):
        result = subprocess.run([
            ARGS.ffprobe, "-v", "error", "-select_streams", "a:0", "-show_streams",
            "-of", "json", str(media),
        ], check=True, capture_output=True, timeout=10)
        return json.loads(result.stdout)["streams"][0]

    def valid_codec_frames(self, media, codec, duration, native_rate):
        if codec in ("aac", "libopus"):
            stream = self.audio_stream(media)
            frames = round(Fraction(stream["duration_ts"]) * Fraction(stream["time_base"]) * native_rate)
            if codec == "libopus":
                data = media.read_bytes()
                header = data.index(b"OpusHead")
                # Ogg's final granule includes the initial Opus pre-skip.
                frames -= struct.unpack_from("<H", data, header + 10)[0]
            return frames
        # The generated PCM length, and MP3's gapless length, are independent
        # of how an installed FFmpeg decoder handles a final partial AAC packet.
        return round(Fraction(duration) * 44100)

    def checked_samples(self, handle, start, count, channels=1):
        result, samples, error = self.read(handle, start, count, channels)
        self.assertEqual(count, result, error)
        self.assertIsNone(error)
        self.assertNotIn(-23456, samples, "A low-amplitude fixture must overwrite every output sample")
        return samples

    def assert_signal_preserved(self, expected, actual, sample_rate, channels=1):
        self.assertEqual(len(expected), len(actual))
        frames = len(expected) // channels
        window = math.ceil(Fraction(2 * sample_rate, 997))
        for offset in range(0, frames, window):
            # Anchor the last window at EOF: a valid prefix must not hide a
            # decoder shortfall replaced by a silent suffix. Respect genuine
            # padding whenever the full decode has no signal in that window.
            start = min(offset, max(0, frames - window))
            end = min(start + window, frames)
            for channel in range(channels):
                reference = expected[start * channels + channel:end * channels:channels]
                samples = actual[start * channels + channel:end * channels:channels]
                with self.subTest(window_start=start, channel=channel):
                    if any(value > 0 for value in reference):
                        self.assertTrue(any(value > 0 for value in samples), "Positive tone samples were lost")
                    if any(value < 0 for value in reference):
                        self.assertTrue(any(value < 0 for value in samples), "Negative tone samples were lost")

    def ffmpeg_samples(self, media, sample_rate, internal_float=False):
        result = subprocess.run([
            ARGS.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(media),
            *(["-af", f"aresample={sample_rate}:internal_sample_fmt=fltp"] if internal_float else []),
            "-ar", str(sample_rate), "-c:a", "pcm_s16le", "-f", "s16le", "-",
        ], check=True, capture_output=True, timeout=10)
        self.assertEqual(b"", result.stderr)
        return list(struct.unpack(f"<{len(result.stdout) // 2}h", result.stdout))

    def test_valid_codecs_and_resampled_eof_match_real_output_length(self):
        for codec, extension, native_rate in [
            ("pcm_s16le", "wav", 44100), ("flac", "flac", 44100),
            ("libmp3lame", "mp3", 44100), ("aac", "m4a", 44100),
            ("libopus", "opus", 48000),
        ]:
            for duration in ("0.003", "1.013", "3.117"):
                media = self.generated_codec_media(codec, extension, duration)
                for rate in dict.fromkeys((native_rate, 32000, 48000)):
                    with self.subTest(codec=codec, duration=duration, rate=rate):
                        native_frames = self.valid_codec_frames(media, codec, duration, native_rate)
                        count = math.ceil(Fraction(native_frames * rate, native_rate))
                        with self.opened(media, sample_rate=rate, expected_format=(rate, 2, 2)) as (handle, info):
                            self.assertEqual(count, info.total_samples)
                            self.assertEqual(count, info.decoded_samples)
                            self.assertEqual(0, info.delay_samples)
                            result, whole, error = self.read(handle, 0, count, 2)
                            self.assertEqual(count, result, error)
                            self.assertIsNone(error)
                            self.assertNotEqual([0] * len(whole), whole)
                            # Check each EOF edge, a singleton and a backwards seek.
                            for start, length in [(count - 1, 1), (max(0, count - 137), min(count, 137)),
                                                  (0, min(count, 57)), (count // 2, min(64, count - count // 2)),
                                                  (count, 17), (-13, min(26, count + 13))]:
                                got, samples, error = self.read(handle, start, length, 2)
                                self.assertEqual(length, got, error)
                                self.assertIsNone(error)
                                if start >= count:
                                    self.assertEqual([0] * (length * 2), samples)
                                if start < 0:
                                    self.assertEqual([0] * (-start * 2), samples[: -start * 2])
                        # A new handle follows the same full decode order. This
                        # tests chunk boundaries without imposing bit-exact lossy
                        # random access on decoder synthesis/PNS state.
                        with self.opened(media, sample_rate=rate, expected_format=(rate, 2, 2)) as (handle, _):
                            sequential = []
                            for start in range(0, count, 4093):
                                length = min(4093, count - start)
                                got, samples, error = self.read(handle, start, length, 2)
                                self.assertEqual(length, got, error)
                                sequential.extend(samples)
                            self.assert_samples_equal(whole, sequential)

    def test_fresh_aac_and_opus_middle_and_near_eof_reads_recover_to_head(self):
        for codec, extension, native_rate in [("aac", "m4a", 44100), ("libopus", "opus", 48000)]:
            media = self.generated_codec_media(codec, extension, "1.013")
            native_frames = self.valid_codec_frames(media, codec, "1.013", native_rate)
            for requested_rate in (0, 32000, 44100):
                rate = requested_rate or native_rate
                count = math.ceil(Fraction(native_frames * rate, native_rate))
                with self.opened(media, sample_rate=requested_rate, expected_format=(rate, 2, 2)) as (handle, info):
                    self.assertEqual(count, info.total_samples)
                    reference = self.checked_samples(handle, 0, count, 2)
                for start in (count // 2, count - 137):
                    with self.subTest(codec=codec, requested_rate=requested_rate, first_start=start):
                        # Nothing is read on this handle before the first seek.
                        with self.opened(media, sample_rate=requested_rate, expected_format=(rate, 2, 2)) as (handle, info):
                            self.assertEqual(count, info.total_samples)
                            actual = self.checked_samples(handle, start, count - start, 2)
                            if codec == "aac":
                                # PNS is disabled when encoding this fixture;
                                # all requested samples must match, not just its tone.
                                self.assert_samples_equal(reference[start * 2:], actual)
                            else:
                                self.assert_signal_preserved(reference[start * 2:], actual, rate, 2)
                            self.assertEqual([0] * 34, self.checked_samples(handle, count, 17, 2))
                            self.checked_samples(handle, 0, 128, 2)
                            recovered = self.checked_samples(handle, 0, count, 2)
                            self.assert_samples_equal(reference, recovered)
                            tail = self.checked_samples(handle, count - 137, 154, 2)
                            if codec == "aac":
                                self.assert_samples_equal(reference[-274:], tail[:274])
                            else:
                                self.assert_signal_preserved(reference[-274:], tail[:274], rate, 2)
                            self.assertEqual([0] * 34, tail[274:])

    def test_resampled_lossless_random_reads_match_whole_decode(self):
        for codec, extension in [("pcm_s16le", "wav"), ("flac", "flac")]:
            media = self.generated_codec_media(codec, extension, "3.117")
            for rate in (32000, 48000):
                with self.subTest(codec=codec, rate=rate):
                    with self.opened(media, sample_rate=rate, expected_format=(rate, 2, 2)) as (handle, info):
                        result, whole, error = self.read(handle, 0, info.total_samples, 2)
                        self.assertEqual(info.total_samples, result, error)
                        for start, count in [(info.total_samples - 1, 1), (197, 1024),
                                             (info.total_samples - 137, 137), (1, 313), (0, 57)]:
                            result, samples, error = self.read(handle, start, count, 2)
                            self.assertEqual(count, result, error)
                            self.assertEqual(whole[start * 2:(start + count) * 2], samples)
                    for start in (len(whole) // 4, len(whole) // 2 - 137):
                        with self.opened(media, sample_rate=rate, expected_format=(rate, 2, 2)) as (handle, info):
                            samples = self.checked_samples(handle, start, info.total_samples - start, 2)
                            self.assert_samples_equal(whole[start * 2:], samples)

    def test_resampled_truncated_tail_is_not_replaced_by_filter_padding(self):
        media = self.directory / "resampled-truncated.wav"
        shutil.copyfile(self.source, media)
        with self.opened(media, sample_rate=32000, expected_format=(32000, 1, 2)) as (handle, info):
            result, _, error = self.read(handle, 0, 64)
            self.assertEqual(64, result, error)
            with media.open("r+b") as stream:
                stream.truncate(media.stat().st_size - 64)
            for _ in range(2):
                result, _, error = self.read(handle, info.total_samples - 256, 256)
                self.assertEqual(-1, result)
                self.assertTrue(error)
            with self.source.open("rb") as source, media.open("r+b") as destination:
                shutil.copyfileobj(source, destination)
            result, samples, error = self.read(handle, info.total_samples - 256, 256)
            self.assertEqual(256, result, error)
            self.assertIsNone(error)
            self.assertNotEqual([0] * 256, samples)

    def test_aac_priming_is_not_applied_twice_to_video_sync(self):
        for video_delay, audio_delay, expected_delay in [(0, 0, 0), (0, 0.24, 11520), (0.24, 0, -11520)]:
            with self.subTest(delay=expected_delay):
                media = self.directory / f"aac-sync-{expected_delay}.mp4"
                subprocess.run([
                    ARGS.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-itsoffset", str(video_delay), "-f", "lavfi", "-i", "color=s=16x16:r=25:d=2",
                    "-itsoffset", str(audio_delay), "-i", str(self.source),
                    "-map", "0:v", "-map", "1:a", "-c:v", "mpeg4", "-fps_mode", "passthrough", "-c:a", "aac", "-aac_pns", "0", "-t", "1.2", str(media),
                ], check=True, timeout=10)
                metadata = subprocess.run([
                    ARGS.ffprobe, "-v", "error", "-show_streams", "-of", "json", str(media),
                ], check=True, capture_output=True, timeout=10)
                streams = json.loads(metadata.stdout)["streams"]
                starts = {stream["codec_type"]: Fraction(stream["start_pts"]) * Fraction(stream["time_base"])
                          for stream in streams}
                # The muxer's edit list decides the actual starts. In particular,
                # positive AAC offsets may retain the coded priming packet.
                audio = next(stream for stream in streams if stream["codec_type"] == "audio")
                valid_frames = round(Fraction(audio["duration_ts"]) * Fraction(audio["time_base"]) * 48000)
                for requested_rate in (0, 32000, 44100):
                    rate = requested_rate or 48000
                    expected_delay = math.ceil((starts["audio"] - starts["video"]) * rate)
                    expected_frames = math.ceil(Fraction(valid_frames * rate, 48000))
                    with self.subTest(rate=rate, delay=expected_delay):
                        with self.opened(media, av_sync=0, sample_rate=requested_rate,
                                         expected_format=(rate, 1, 2)) as (handle, unshifted):
                            self.assertEqual(expected_frames, unshifted.total_samples)
                            self.assertEqual(0, unshifted.delay_samples)
                            original = self.checked_samples(handle, 0, unshifted.total_samples)
                        with self.opened(media, sample_rate=requested_rate,
                                         expected_format=(rate, 1, 2)) as (handle, info):
                            self.assertEqual(expected_delay, info.delay_samples)
                            self.assertEqual(expected_frames, info.decoded_samples)
                            self.assertEqual(expected_frames + expected_delay, info.total_samples)
                            # The first full read catches negative-offset priming;
                            # positive MOV reads also exercise the constructor's
                            # tail probe followed by rewind from demuxer EOF.
                            actual = self.checked_samples(handle, 0, info.total_samples)
                            expected = ([0] * expected_delay + original if expected_delay >= 0
                                        else original[-expected_delay:])
                            self.assert_samples_equal(expected, actual)
                            self.assertEqual([0] * 17, self.checked_samples(handle, info.total_samples, 17))
                            self.checked_samples(handle, info.total_samples - 137, 137)
                            self.assert_samples_equal(expected, self.checked_samples(handle, 0, info.total_samples))
                        with self.opened(media, sample_rate=requested_rate,
                                         expected_format=(rate, 1, 2)) as (handle, info):
                            tail = self.checked_samples(handle, info.total_samples - 137, 154)
                            self.assert_signal_preserved(expected[-137:], tail[:137], rate)
                            self.assertEqual([0] * 17, tail[137:])

    def test_internal_timestamp_gap_keeps_existing_contiguous_pcm_timeline(self):
        # Native does not expose AviSynth's fill_agaps option. Its default
        # timeline contains the decoded packets, without adding synthetic silence.
        frames, gap_start, gap_end = 192000, 48 * 1024, 72 * 1024
        source_samples = [1000 + (index * 7) % 2047 for index in range(frames)]
        expected = source_samples[:gap_start] + source_samples[gap_end:]
        source = self.directory / "gap-source.wav"
        compact = self.directory / "gap-compacted.wav"
        for path, samples in [(source, source_samples), (compact, expected)]:
            with wave.open(str(path), "wb") as stream:
                stream.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
                stream.writeframes(struct.pack(f"<{len(samples)}h", *samples))
        media = self.directory / "internal-gap.mkv"
        subprocess.run([
            ARGS.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
            "-af", "asetnsamples=n=1024:p=0,aselect=not(between(n\\,48\\,71))",
            "-c:a", "pcm_s16le", str(media),
        ], check=True, timeout=10)
        packet_result = subprocess.run([
            ARGS.ffprobe, "-v", "error", "-show_packets", "-of", "json", str(media),
        ], check=True, capture_output=True, timeout=10)
        packets = json.loads(packet_result.stdout)["packets"]
        gaps = [Fraction(right["pts_time"]) - Fraction(left["pts_time"]) - Fraction(left["duration_time"])
                for left, right in zip(packets, packets[1:])]
        self.assertEqual(1, sum(gap > Fraction(1, 2) for gap in gaps))
        for requested_rate in (0, 32000, 44100):
            rate = requested_rate or 48000
            # PCM-only FFmpeg resampling is independent of MOV edit lists,
            # codec priming, and the compressed decoder under test.
            reference = expected if rate == 48000 else self.ffmpeg_samples(compact, rate, internal_float=True)
            with self.subTest(rate=rate):
                with self.opened(media, av_sync=0, sample_rate=requested_rate,
                                 expected_format=(rate, 1, 2)) as (handle, info):
                    self.assertEqual(len(reference), info.total_samples)
                    self.assertEqual(0, info.delay_samples)
                    whole = self.checked_samples(handle, 0, info.total_samples)
                    self.assert_samples_equal(reference, whole)
                boundary = gap_start * rate // 48000
                for start in (boundary - 128, boundary, boundary + 128):
                    with self.opened(media, av_sync=0, sample_rate=requested_rate,
                                     expected_format=(rate, 1, 2)) as (handle, _):
                        self.assert_samples_equal(reference[start:start + 256], self.checked_samples(handle, start, 256))

    def test_strong_downsampling_counts_and_returns_the_full_eof(self):
        for frames in (264, 192000):
            with self.subTest(frames=frames):
                media = self.directory / f"high-rate-{frames}.wav"
                with wave.open(str(media), "wb") as stream:
                    stream.setparams((1, 2, 192000, 0, "NONE", "not compressed"))
                    stream.writeframes(struct.pack(f"<{frames}h", *([1234] * frames)))
                expected = self.ffmpeg_samples(media, 8000, internal_float=True)
                if not expected:
                    # This sub-filter-width duration legitimately produces no
                    # resampler output; opening must report that empty timeline.
                    options = Options(stream_index=-1, threads=1, av_sync=0, ff_loglevel=-8,
                                      cache_index=0, sample_format=1, sample_rate=8000)
                    error = c.c_void_p()
                    handle = self.native.lsmas_audio_open_utf8(str(media.resolve()).encode(), c.byref(options), c.byref(error))
                    if handle:
                        self.native.lsmas_audio_close(handle)
                    self.assertFalse(handle)
                    self.assertTrue(self.error_text(error))
                else:
                    with self.opened(media, sample_rate=8000, expected_format=(8000, 1, 2)) as (handle, info):
                        self.assertEqual(len(expected), info.total_samples)
                        result, samples, error = self.read(handle, 0, info.total_samples)
                        self.assertEqual(info.total_samples, result, error)
                        self.assert_samples_equal(expected, samples)


def main():
    global ARGS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-library", type=pathlib.Path, required=True)
    parser.add_argument("--artifacts-dir", type=pathlib.Path, default=pathlib.Path("artifacts/native-audio-tests"))
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    ARGS = parser.parse_args()
    ARGS.artifacts_dir.mkdir(parents=True, exist_ok=True)
    if ARGS.child:
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(NativeAudioReadTests))
        return 0 if result.wasSuccessful() else 1
    try:
        return subprocess.run([sys.executable, __file__, *sys.argv[1:], "--child"], timeout=90, check=False).returncode
    except subprocess.TimeoutExpired:
        print("Native audio tests exceeded the 90 second total timeout", file=sys.stderr)
        return 124


if __name__ == "__main__":
    sys.exit(main())
