# LsmasSharp

LsmasSharp is a .NET video/audio provider that ports the `LWLibavSource` and `LWLibavAudioSource` behavior from L-SMASH-Works into a standalone library. It does not require VapourSynth at runtime. The managed NuGet package is native-free; applications load `lsmasnative` explicitly when they need decoding.

The native build currently targets Windows x64 and links against the FFmpeg `lsmas` fork used by L-SMASH-Works.

## Native audio read contract

Patched HOE builds exclude declared codec priming and
trailing padding, preserve A/V alignment after that trim, and include the actual
samples emitted when the output resampler drains at EOF. Reads outside this
timeline still return successful silence. A decoder that cannot produce the
requested in-range samples returns `-1`; discard the entire destination buffer
and retry the complete range if appropriate.

Fresh random reads apply indexed codec priming once, including AAC and Opus
after a negative A/V offset. MOV tail probing and rewinding preserve the same
sample range when audio begins after video. The FFmpeg MOV patch calculates
the stream end from its start plus duration instead of treating duration as an
absolute timestamp.

Returning to the first AAC packet reinitializes a previously used decoder:
AAC flush alone preserves window history that can change retained priming
samples. Middle seeks and sequential reads retain their usual decoder path.

The shared build entry applies both maintained patches to private source copies.
HOE sources are prepared under the output's `hoe-source` directory; Zig only compiles
and links them. A patch mismatch stops the build without editing the submodule.
HOE and legacy builds both report API 1.1.0: it describes the exported interface,
which these decoder fixes do not change. Identify the implementation and fixes
through the native release/version, Git commit and build manifest. Older DLLs
with the same API version can still contain the audio defects described above;
legacy L-SMASH-Works builds do not include the HOE audio patches.

## Reproducible HOE builds

The `third_party/l-smash-works` submodule pins release `1310.0.0.0`
(`7e65185d3f08`). Its nested FFmpeg submodule pins `39c24f36247f`; no second
FFmpeg version setting is needed. Keep these source checkouts unchanged:
maintained HOE and FFmpeg patches are applied to private build copies.

Initialize the dependencies and build from the repository root:

```powershell
git submodule update --init --recursive
python scripts/build_hoe_native.py
```

Install the exact Zig version declared at the top of `scripts/build_hoe_native.py`, Python
3.10 or later, and Git. Windows also requires an Ubuntu 24.04 WSL distribution;
GHA uses Ubuntu 24.04 with the same Python entry point and FFmpeg build script.
The FFmpeg script downloads its MinGW tools and builds the pinned dav1d/zlib
versions. Ubuntu security/package updates can still change the MinGW toolchain;
this is a common source baseline and build recipe, not a bit-identical container.
The default output is `artifacts/native/win-x64-hoe`. `scripts/build_win64.ps1`
defaults to HOE and also builds the managed solution; explicit `-Variant AkarinVS`
retains the separate legacy workflow.

Use this one entry point for HOE. It checks the submodule pins, reuses or builds
FFmpeg, prepares the patched HOE sources, then calls Zig. Tool versions and both
patch paths are declared together in the Python script. The older Zig wrapper
scripts serve the Akarin workflow; direct Zig builds require prepared HOE sources.

The default command automatically reuses verified FFmpeg libraries. Only FFmpeg
source, its patch, dav1d/zlib versions and the FFmpeg recipe invalidate that cache;
HOE, xxHash, Zig and Native changes rebuild the DLL without rebuilding FFmpeg.
`--skip-ffmpeg` requires a valid cache; `--rebuild-ffmpeg` forces a rebuild.
The generated `build-manifest.json` records both patch hashes and the DLL hash.
GHA calls the same entry point and tests the resulting DLL on Windows.

Lossy decoder synthesis state can make independently sought PCM differ from a
continuous decode, notably AAC perceptual noise substitution. This contract
preserves valid sample ranges and reports real failures; it does not promise
bit-identical synthesis across arbitrary decoder seeks.

## Native audio regression tests

After building the native DLL, run the following from the repository root with
Python 3, FFmpeg and FFprobe on `PATH`:

```powershell
python scripts/test_native_audio_reads.py --native-library artifacts/native/win-x64-hoe/lsmasnative.dll
```

The tests generate PCM, FLAC, MP3, AAC, Opus and delayed A/V media under
`artifacts`. They verify output lengths using container timing and independent
FFmpeg decoding as appropriate across sub-frame, one-second and longer inputs;
compare full reads with sequential
chunks; check exact lossless random reads and resampler EOF; and truncate only
their temporary media to verify persistent errors and same-handle recovery.
AAC sync fixtures use FFprobe's actual muxed stream starts and preserve the
existing default-PNS decoding cases separately from exact PCM comparisons.
Fresh AAC/Opus middle and near-EOF reads cover default and resampled output;
the PNS-disabled AAC controls require exact PCM. The complete
test process has a 90 second timeout. Use `--native-library` to select another
build and `--artifacts-dir` to choose a different output directory.

## License

This repository is licensed as `GPL-3.0-or-later`. See `LICENSE` and `THIRD_PARTY_NOTICES.md`.
