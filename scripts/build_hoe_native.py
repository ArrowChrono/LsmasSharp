"""Build the pinned HOE Windows DLL with the shared Linux/WSL recipe."""

import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
VERSIONS = {"zig_version": "0.17.0-dev.2131+d08989840", "dav1d_version": "1.5.3", "zlib_version": "1.3.1"}
HOE_SOURCE = "third_party/l-smash-works"
FFMPEG_PATCH = "src/LsmasNative/patches/ffmpeg-mov-audio-end.patch"
HOE_PATCH = "src/LsmasNative/patches/hoe-audio-read-contract.patch"
FFMPEG_SCRIPT = "scripts/build_ffmpeg_lsmas_win64_local_mingw_static.sh"
FFMPEG_PREFIX = "build-deps/ffmpeg-hoe-win64-static"
NATIVE_OUTPUT = "artifacts/native/win-x64-hoe"
ARCHIVES = ("libavcodec.a", "libavformat.a", "libavutil.a", "libswscale.a", "libswresample.a", "libdav1d.a", "libz.a")
CONTRACT = "#pragma once\n#define LSMAS_FFMPEG_MOV_AUDIO_END_FIXED 1\n"


def run(*command, input=None):
    subprocess.run([str(arg) for arg in command], cwd=ROOT, input=input, check=True)


def git(directory, *args):
    return subprocess.check_output(["git", "-C", str(directory), *args], text=True).strip()


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def text_sha256(path):
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def pinned_submodule(parent, relative):
    entry = git(parent, "ls-files", "--stage", "--", relative).split()
    if len(entry) < 4 or entry[0] != "160000":
        raise ValueError(f"Missing pinned submodule: {relative}")
    source = parent / relative
    if not (source / ".git").exists():
        raise ValueError("Initialize native sources with git submodule update --init --recursive")
    actual = git(source, "rev-parse", "HEAD")
    if actual != entry[1]:
        raise ValueError(f"Submodule {relative} is at {actual}, expected gitlink {entry[1]}")
    # The compiled nested submodules are checked separately, without scanning twice.
    result = subprocess.run(["git", "-C", str(source), "diff", "--quiet", "--ignore-submodules=all", "HEAD", "--"])
    if result.returncode:
        raise ValueError(f"Submodule {relative} has source changes; patches belong in the private build copy")
    return source, actual


def build_inputs():
    hoe, hoe_commit = pinned_submodule(ROOT, HOE_SOURCE)
    ffmpeg, ffmpeg_commit = pinned_submodule(hoe, "FFmpeg")
    _, xxhash_commit = pinned_submodule(hoe, "xxHash")
    return dict(VERSIONS, hoe_source=HOE_SOURCE, hoe_commit=hoe_commit, ffmpeg_commit=ffmpeg_commit,
                xxhash_commit=xxhash_commit, ffmpeg_source=ffmpeg.relative_to(ROOT).as_posix(),
                ffmpeg_patch=FFMPEG_PATCH, hoe_patch=HOE_PATCH,
                ffmpeg_patch_sha256=text_sha256(ROOT / FFMPEG_PATCH),
                hoe_patch_sha256=text_sha256(ROOT / HOE_PATCH),
                build_script_sha256=text_sha256(ROOT / FFMPEG_SCRIPT))


def ffmpeg_inputs(config):
    # Only inputs which change these static libraries belong in their cache key.
    return {key: config[key] for key in ("ffmpeg_commit", "ffmpeg_patch_sha256", "dav1d_version",
                                         "zlib_version", "build_script_sha256")}


def build_key(inputs):
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


def prefix_contents(prefix):
    return {name: sha256(prefix / "lib" / name) for name in ARCHIVES}


def verify_prefix(prefix, expected):
    marker = prefix / "lsmas-build.json"
    if not marker.is_file():
        raise ValueError("FFmpeg prefix has no build identity; rebuild it with scripts/build_hoe_native.py")
    actual = json.loads(marker.read_text(encoding="utf-8"))
    if actual.get("inputs") != expected:
        raise ValueError("FFmpeg prefix does not match the pinned sources, dependencies or patch; rebuild it")
    if actual.get("archives") != prefix_contents(prefix):
        raise ValueError("FFmpeg prefix libraries do not match their recorded build; rebuild it")
    if (prefix / "include/lsmas_ffmpeg_contract.h").read_text() != CONTRACT:
        raise ValueError("FFmpeg prefix is missing the MOV audio-end contract")


def write_prefix_stamp(prefix, expected):
    archives = prefix_contents(prefix)
    (prefix / "include/lsmas_ffmpeg_contract.h").write_text(CONTRACT, encoding="utf-8", newline="\n")
    (prefix / "lsmas-build.json").write_text(json.dumps({"inputs": expected, "archives": archives}, indent=2) + "\n", encoding="utf-8")


def output_path(value):
    path = (ROOT / value).resolve()
    if not any(base in path.parents for base in (ROOT / "build-deps", ROOT / "artifacts")):
        raise ValueError("Build outputs must be below this repository's build-deps or artifacts directory")
    return path


def build_ffmpeg(config, prefix, jobs):
    def host_path(path):
        if sys.platform == "win32":
            return subprocess.check_output(["wsl", "--exec", "wslpath", "-a", pathlib.Path(path).as_posix()], text=True).strip()
        return str(path)

    environment = {
        "LSMAS_FFMPEG_SOURCE": host_path(ROOT / config["ffmpeg_source"]),
        "LSMAS_FFMPEG_PATCH": host_path(ROOT / config["ffmpeg_patch"]),
        "LSMAS_DAV1D_VERSION": config["dav1d_version"],
        "LSMAS_ZLIB_VERSION": config["zlib_version"],
    }
    command = ["env", *(f"{key}={value}" for key, value in environment.items()), "bash",
               host_path(ROOT / FFMPEG_SCRIPT), host_path(prefix), str(jobs), "0",
               "https://github.com/HomeOfAviSynthPlusEvolution/FFmpeg", config["ffmpeg_commit"]]
    if sys.platform == "win32":
        command = ["wsl", "--exec", *command]
    # The recipe starts with an empty prefix and privately checks out the pinned
    # commit, applies the patch, then compiles/installs. Failure never reaches stamp.
    run(*command)
    write_prefix_stamp(prefix, ffmpeg_inputs(config))


def prepare_hoe(config, native_output):
    source = ROOT / config["hoe_source"]
    output = output_path(native_output) / "hoe-source"
    if output.exists():
        shutil.rmtree(output)
    for directory in ("common", "xxHash"):
        shutil.copytree(source / directory, output / directory, ignore=shutil.ignore_patterns(".git"))
    # Git patches use LF; leave binary files in the source copy intact.
    for path in output.rglob("*"):
        if path.suffix in (".c", ".h") and path.is_file():
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
    patch = (ROOT / config["hoe_patch"]).read_text(encoding="utf-8").encode("utf-8")
    run("git", "-c", "core.autocrlf=false", "apply", "--unidiff-zero",
        f"--directory={output.relative_to(ROOT).as_posix()}", "-", input=patch)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zig", default="zig")
    parser.add_argument("--prefix", default=FFMPEG_PREFIX)
    parser.add_argument("--out-dir", default=NATIVE_OUTPUT)
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    parser.add_argument("--configuration", choices=("Debug", "Release"), default="Release")
    parser.add_argument("--github-env", action="store_true", help="Print pinned CI settings without building")
    cache = parser.add_mutually_exclusive_group()
    cache.add_argument("--skip-ffmpeg", action="store_true", help="Require an already verified prefix")
    cache.add_argument("--rebuild-ffmpeg", action="store_true")
    args = parser.parse_args()
    config = build_inputs()
    ffmpeg_config = ffmpeg_inputs(config)
    if args.github_env:
        for key, value in {"ZIG_VERSION": config["zig_version"], "FFMPEG_BUILD_KEY": build_key(ffmpeg_config),
                           "FFMPEG_PREFIX": args.prefix, "OUT_DIR": args.out_dir}.items():
            print(f"{key}={value}")
        return
    zig = shutil.which(args.zig)
    if not zig:
        parser.error(f"Install Zig {config['zig_version']} or pass --zig")
    zig = pathlib.Path(zig).resolve()
    version = subprocess.check_output([str(zig), "version"], text=True).strip()
    if version != config["zig_version"]:
        parser.error(f"Zig {version} does not match required {config['zig_version']}")
    prefix, output = output_path(args.prefix), output_path(args.out_dir)
    if args.rebuild_ffmpeg:
        rebuild = True
    else:
        try:
            verify_prefix(prefix, ffmpeg_config)
            rebuild = False
        except (ValueError, OSError) as error:
            if args.skip_ffmpeg:
                raise
            print(f"Building FFmpeg: {error}", flush=True)
            rebuild = True
    if rebuild:
        build_ffmpeg(config, prefix, args.jobs)
    else:
        print("Reusing verified FFmpeg libraries", flush=True)
    prepared = prepare_hoe(config, output)
    output.mkdir(parents=True, exist_ok=True)
    metadata = {
        "lsmasnative_version": git(ROOT, "describe", "--tags", "--always", "--dirty"),
        "lsmasnative_git_head": git(ROOT, "rev-parse", "--short=12", "HEAD"),
        "lsmasnative_zig_version": version,
        "lsmashworks_git_url": "https://github.com/HomeOfAviSynthPlusEvolution/L-SMASH-Works.git",
        "lsmashworks_git_head": config["hoe_commit"][:12],
        "lsmashworks_git_branch": config["hoe_commit"],
        "ffmpeg_git_url": "https://github.com/HomeOfAviSynthPlusEvolution/FFmpeg",
        "ffmpeg_git_head": config["ffmpeg_commit"][:12] + "+mov-audio-end",
        "ffmpeg_git_branch": "custom-patches-for-lsmashsource",
        "dav1d_version": config["dav1d_version"],
        "zlib_version": config["zlib_version"],
    }
    run(zig, "build", "--build-file", ROOT / "src/LsmasNative/build.zig", "--prefix", output / "install",
        "-Dtarget=x86_64-windows-gnu", "-Dffmpeg_static=true",
        "-Doptimize=" + ("ReleaseFast" if args.configuration == "Release" else "Debug"),
        f"-Dffmpeg_prefix={prefix}", f"-Dl_smash_works_dir={prepared}",
        *(f"-D{key}={value}" for key, value in metadata.items()))
    for name in ("lsmasnative.dll", "lsmasnative.pdb"):
        shutil.copy2(output / "install/bin" / name, output / name)
    manifest = dict(config, native=metadata, dll_sha256=sha256(output / "lsmasnative.dll"))
    (output / "build-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Built {output.relative_to(ROOT).as_posix()}/lsmasnative.dll")


if __name__ == "__main__":
    main()
