"""Check native build preparation and provenance in temporary directories."""

import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("build_hoe_native", pathlib.Path(__file__).with_name("build_hoe_native.py"))
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class NativeBuildTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="native-build-")
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name)
        self.prefix = self.root / "prefix"
        (self.prefix / "lib").mkdir(parents=True)
        (self.prefix / "include").mkdir()
        self.inputs = {"ffmpeg_commit": "a" * 40, "ffmpeg_patch_sha256": "patch-hash",
                       "dav1d_version": "test-dav1d", "zlib_version": "test-zlib",
                       "build_script_sha256": "recipe-hash"}
        self.config = dict(self.inputs, hoe_commit="b" * 40, xxhash_commit="c" * 40,
                           zig_version="test-zig", hoe_source="third_party/hoe",
                           ffmpeg_source="third_party/hoe/FFmpeg", ffmpeg_patch="audio.patch",
                           hoe_patch="hoe.patch", hoe_patch_sha256="hoe-patch-hash")
        for name in build.ARCHIVES:
            (self.prefix / "lib" / name).write_bytes(b"archive:" + name.encode())
        root_patch = mock.patch.object(build, "ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)

    def test_matching_stamp_accepts_archives_and_writes_decoder_contract(self):
        build.write_prefix_stamp(self.prefix, self.inputs)
        marker = json.loads((self.prefix / "lsmas-build.json").read_text())
        self.assertEqual(self.inputs, marker["inputs"])
        self.assertEqual({name: hashlib.sha256(b"archive:" + name.encode()).hexdigest()
                          for name in build.ARCHIVES}, marker["archives"])
        self.assertEqual("#pragma once\n#define LSMAS_FFMPEG_MOV_AUDIO_END_FIXED 1\n",
                         (self.prefix / "include/lsmas_ffmpeg_contract.h").read_text())
        build.verify_prefix(self.prefix, self.inputs)

    def test_missing_or_old_stamp_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "no build identity"):
            build.verify_prefix(self.prefix, self.inputs)
        for old in ({}, {"inputs": dict(self.inputs, build_script_sha256="previous-recipe")},
                    {"inputs": dict(self.inputs, ffmpeg_commit="b" * 40)}):
            with self.subTest(marker=old):
                (self.prefix / "lsmas-build.json").write_text(json.dumps(old))
                with self.assertRaisesRegex(ValueError, "does not match the pinned"):
                    build.verify_prefix(self.prefix, self.inputs)

    def test_replaced_or_missing_archive_is_rejected(self):
        build.write_prefix_stamp(self.prefix, self.inputs)
        archive = self.prefix / "lib/libavformat.a"
        original = archive.read_bytes()
        archive.write_bytes(original + b"different-MOV-code")
        with self.assertRaisesRegex(ValueError, "libraries do not match"):
            build.verify_prefix(self.prefix, self.inputs)
        archive.write_bytes(original)
        build.verify_prefix(self.prefix, self.inputs)
        archive.unlink()
        with self.assertRaises(FileNotFoundError):
            build.verify_prefix(self.prefix, self.inputs)

    def test_missing_or_disabled_decoder_contract_is_rejected(self):
        build.write_prefix_stamp(self.prefix, self.inputs)
        header = self.prefix / "include/lsmas_ffmpeg_contract.h"
        header.write_text("#define LSMAS_FFMPEG_MOV_AUDIO_END_FIXED 0\n")
        with self.assertRaisesRegex(ValueError, "missing the MOV audio-end contract"):
            build.verify_prefix(self.prefix, self.inputs)
        header.unlink()
        with self.assertRaises(FileNotFoundError):
            build.verify_prefix(self.prefix, self.inputs)

    def test_ffmpeg_build_failure_does_not_stamp_the_prefix(self):
        with mock.patch.object(build.sys, "platform", "linux"), \
                mock.patch.object(build, "run", side_effect=subprocess.CalledProcessError(1, ["bash", "build"])), \
                mock.patch.object(build, "write_prefix_stamp", wraps=build.write_prefix_stamp) as stamp:
            with self.assertRaises(subprocess.CalledProcessError):
                build.build_ffmpeg(self.config, self.prefix, 2)
            stamp.assert_not_called()
        self.assertFalse((self.prefix / "lsmas-build.json").exists())
        self.assertFalse((self.prefix / "include/lsmas_ffmpeg_contract.h").exists())

    def test_ffmpeg_build_stamps_only_after_the_shell_succeeds(self):
        def shell_succeeds(*command, **kwargs):
            self.assertFalse((self.prefix / "lsmas-build.json").exists())
            self.assertFalse((self.prefix / "include/lsmas_ffmpeg_contract.h").exists())

        with mock.patch.object(build.sys, "platform", "linux"), \
                mock.patch.object(build, "run", side_effect=shell_succeeds) as shell:
            build.build_ffmpeg(self.config, self.prefix, 2)
            shell.assert_called_once()
        build.verify_prefix(self.prefix, self.inputs)

    def test_submodule_head_must_match_gitlink_and_sources_must_be_clean(self):
        source = self.root / "dependency"
        source.mkdir()
        (source / ".git").write_text("gitdir: metadata\n")
        expected_head = "a" * 40
        cases = [(expected_head, 0, None), ("b" * 40, 0, "expected gitlink"),
                 (expected_head, 1, "has source changes")]
        for actual_head, diff_status, error in cases:
            with self.subTest(head=actual_head, diff_status=diff_status), \
                    mock.patch.object(build, "git", side_effect=[f"160000 {expected_head} 0\tdependency", actual_head]), \
                    mock.patch.object(build.subprocess, "run", return_value=subprocess.CompletedProcess([], diff_status)):
                if error:
                    with self.assertRaisesRegex(ValueError, error):
                        build.pinned_submodule(self.root, "dependency")
                else:
                    self.assertEqual((source, expected_head), build.pinned_submodule(self.root, "dependency"))

    def test_unpinned_or_uninitialized_dependency_is_rejected(self):
        for entry in ("", "100644 " + "a" * 40 + " 0\tdependency"):
            with self.subTest(entry=entry), mock.patch.object(build, "git", return_value=entry):
                with self.assertRaisesRegex(ValueError, "Missing pinned submodule"):
                    build.pinned_submodule(self.root, "dependency")
        with mock.patch.object(build, "git", return_value="160000 " + "a" * 40 + " 0\tdependency"):
            with self.assertRaisesRegex(ValueError, "git submodule update"):
                build.pinned_submodule(self.root, "dependency")

    def test_ffmpeg_cache_key_tracks_only_ffmpeg_build_inputs(self):
        original = build.build_key(build.ffmpeg_inputs(self.config))
        self.assertEqual(self.inputs, build.ffmpeg_inputs(self.config))
        self.assertEqual(original, build.build_key(dict(reversed(list(self.inputs.items())))))
        for key in ("hoe_commit", "hoe_patch_sha256", "xxhash_commit", "zig_version"):
            with self.subTest(unchanged_cache_for=key):
                changed = dict(self.config, **{key: "different-native-input"})
                self.assertEqual(original, build.build_key(build.ffmpeg_inputs(changed)))
        for key in self.inputs:
            with self.subTest(invalidated_cache_for=key):
                changed = dict(self.config, **{key: "different-ffmpeg-input"})
                self.assertNotEqual(original, build.build_key(build.ffmpeg_inputs(changed)))

    def test_build_inputs_normalize_text_and_record_both_patches_and_pins(self):
        versions = {"zig_version": "test-zig", "dav1d_version": "test-dav1d", "zlib_version": "test-zlib"}
        files = {"FFMPEG_PATCH": "audio.patch", "HOE_PATCH": "hoe.patch", "FFMPEG_SCRIPT": "build.sh"}
        for relative in files.values():
            (self.root / relative).write_bytes(b"first line\nsecond line\n")
        hoe = self.root / "third_party/hoe"
        pinned = [(hoe, "a" * 40), (hoe / "FFmpeg", "b" * 40), (hoe / "xxHash", "c" * 40)]
        with mock.patch.multiple(build, VERSIONS=versions, HOE_SOURCE="third_party/hoe", **files):
            def read_inputs():
                with mock.patch.object(build, "pinned_submodule", side_effect=pinned):
                    return build.build_inputs()

            original = read_inputs()
            self.assertEqual("third_party/hoe/FFmpeg", original["ffmpeg_source"])
            self.assertEqual("audio.patch", original["ffmpeg_patch"])
            self.assertEqual("hoe.patch", original["hoe_patch"])
            self.assertEqual(("a" * 40, "b" * 40, "c" * 40),
                             (original["hoe_commit"], original["ffmpeg_commit"], original["xxhash_commit"]))
            digest = hashlib.sha256(b"first line\nsecond line\n").hexdigest()
            self.assertEqual([digest] * 3, [original[key] for key in
                             ("ffmpeg_patch_sha256", "hoe_patch_sha256", "build_script_sha256")])
            for relative in files.values():
                (self.root / relative).write_bytes(b"first line\r\nsecond line\r\n")
            self.assertEqual(original, read_inputs())
            for constant, key in (("FFMPEG_PATCH", "ffmpeg_patch_sha256"), ("HOE_PATCH", "hoe_patch_sha256"),
                                  ("FFMPEG_SCRIPT", "build_script_sha256")):
                with self.subTest(changed=constant):
                    path = self.root / files[constant]
                    path.write_bytes(b"changed build input\n")
                    self.assertNotEqual(original[key], read_inputs()[key])
                    path.write_bytes(b"first line\nsecond line\n")

    def hoe_fixture(self):
        source = self.root / self.config["hoe_source"]
        (source / "common").mkdir(parents=True)
        (source / "xxHash").mkdir()
        originals = {"common/audio.c": b"int audio = 1;\r\n", "common/audio.h": b"int audio;\r\n",
                     "xxHash/xxhash.c": b"int hash = 7;\r\n", "common/data.bin": b"\x00\r\n\xff"}
        for relative, data in originals.items():
            (source / relative).write_bytes(data)
        patch = self.root / self.config["hoe_patch"]
        patch.write_text("--- a/common/audio.c\n+++ b/common/audio.c\n@@ -1 +1 @@\n-int audio = 1;\n+int audio = 2;\n",
                         newline="\n")
        return source, originals, patch

    def test_hoe_preparation_patches_a_fresh_copy_and_preserves_source(self):
        source, originals, patch = self.hoe_fixture()
        real_run = subprocess.run
        # A user/global Git conversion policy must not undo the private LF copy.
        with mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.autocrlf",
                                          "GIT_CONFIG_VALUE_0": "true"}), \
                mock.patch.object(build.subprocess, "run", side_effect=lambda *args, **kwargs:
                                  real_run(*args, **dict(kwargs, timeout=10, capture_output=True))):
            prepared = build.prepare_hoe(self.config, "artifacts/native")
            self.assertEqual(self.root / "artifacts/native/hoe-source", prepared)
            self.assertEqual(b"int audio = 2;\n", (prepared / "common/audio.c").read_bytes())
            self.assertEqual(b"int audio;\n", (prepared / "common/audio.h").read_bytes())
            self.assertEqual(b"int hash = 7;\n", (prepared / "xxHash/xxhash.c").read_bytes())
            self.assertEqual(originals["common/data.bin"], (prepared / "common/data.bin").read_bytes())
            (prepared / "common/stale.c").write_text("stale output\n")
            crlf_patch = patch.read_bytes().replace(b"\n", b"\r\n")
            patch.write_bytes(crlf_patch)
            prepared_again = build.prepare_hoe(self.config, "artifacts/native")
            self.assertEqual(prepared, prepared_again)
            self.assertFalse((prepared_again / "common/stale.c").exists())
            self.assertEqual(b"int audio = 2;\n", (prepared_again / "common/audio.c").read_bytes())
            self.assertEqual(crlf_patch, patch.read_bytes())
        self.assertEqual(originals, {relative: (source / relative).read_bytes() for relative in originals})

    def test_hoe_preparation_keeps_other_native_outputs_intact(self):
        self.hoe_fixture()
        real_run = subprocess.run
        with mock.patch.object(build.subprocess, "run", side_effect=lambda *args, **kwargs:
                               real_run(*args, **dict(kwargs, timeout=10, capture_output=True))):
            first = build.prepare_hoe(self.config, "artifacts/first")
            marker = first / "common/first-output-only.c"
            marker.write_text("output still in use\n")
            second = build.prepare_hoe(self.config, self.root / "artifacts/second")
        self.assertNotEqual(first, second)
        self.assertEqual("output still in use\n", marker.read_text())
        self.assertFalse((second / "common/first-output-only.c").exists())
        self.assertEqual(b"int audio = 2;\n", (first / "common/audio.c").read_bytes())
        self.assertEqual(b"int audio = 2;\n", (second / "common/audio.c").read_bytes())

    def test_hoe_preparation_rejects_a_patch_that_does_not_apply(self):
        source, originals, patch = self.hoe_fixture()
        patch.write_text(patch.read_text().replace("-int audio = 1;", "-int audio = 99;"), newline="\n")
        real_run = subprocess.run
        with mock.patch.object(build.subprocess, "run", side_effect=lambda *args, **kwargs:
                               real_run(*args, **dict(kwargs, timeout=10, capture_output=True))):
            with self.assertRaises(subprocess.CalledProcessError):
                build.prepare_hoe(self.config, "artifacts/native")
        self.assertEqual(originals, {relative: (source / relative).read_bytes() for relative in originals})


if __name__ == "__main__":
    unittest.main(verbosity=2)
