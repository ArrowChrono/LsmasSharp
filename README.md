# LsmasSharp

LsmasSharp is a .NET video/audio provider that ports the `LWLibavSource` and `LWLibavAudioSource` behavior from L-SMASH-Works into a standalone library. It does not require VapourSynth at runtime. The managed NuGet package is native-free; applications load `lsmasnative` explicitly when they need decoding.

The native build currently targets Windows x64 and links against the FFmpeg `lsmas` fork used by L-SMASH-Works.

## Native audio read contract

HOE builds expose API 1.1.1. Audio lengths exclude declared codec priming and
trailing padding, preserve A/V alignment after that trim, and include the actual
samples emitted when the output resampler drains at EOF. Reads outside this
timeline still return successful silence. A decoder that cannot produce the
requested in-range samples returns `-1`; discard the entire destination buffer
and retry the complete range if appropriate.

The native build applies `src/LsmasNative/patches/hoe-audio-read-contract.patch`
to a private copy of the HOE sources under the Zig build cache. It does not edit
the dependency checkout. The copy is refreshed on each build; an unsupported
source layout fails patch preparation instead of silently omitting the fix.
Python 3 is needed for HOE builds (`-Dpython=...` selects the interpreter).
Legacy L-SMASH-Works builds continue to report API 1.1.0 in both the version
function and version metadata; they do not claim this audio contract.

Lossy decoder synthesis state can make independently sought PCM differ from a
continuous decode, notably AAC perceptual noise substitution. This contract
preserves valid sample ranges and reports real failures; it does not promise
bit-identical synthesis across arbitrary decoder seeks.

## Native audio regression tests

After building the native DLL, run the following from the repository root with
Python 3, FFmpeg and FFprobe on `PATH`:

```powershell
python scripts/test_native_audio_reads.py --native-library artifacts/native/win-x64/lsmasnative.dll
```

The tests generate PCM, FLAC, MP3, AAC, Opus and delayed A/V media under
`artifacts`. They compare output lengths with independent FFmpeg decoding across
sub-frame, one-second and longer inputs; compare full reads with sequential
chunks; check exact lossless random reads and resampler EOF; and truncate only
their temporary media to verify persistent errors and same-handle recovery.
AAC sync fixtures use FFprobe's actual muxed stream starts and preserve the
existing default-PNS decoding cases separately from exact PCM comparisons. The complete
test process has a 90 second timeout. Use `--native-library` to select another
build and `--artifacts-dir` to choose a different output directory.

## License

This repository is licensed as `GPL-3.0-or-later`. See `LICENSE` and `THIRD_PARTY_NOTICES.md`.
