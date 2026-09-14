"""Prepare the private HOE sources used by the native audio contract build."""
import argparse
import pathlib
import re
import shutil


def apply_patch(root, patch):
    lines = patch.read_text(encoding="utf-8").splitlines(keepends=True)
    i = 0
    while i < len(lines):
        if not lines[i].startswith("--- a/"):
            i += 1
            continue
        relative = pathlib.PurePosixPath(lines[i][6:].strip())
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Patch path must stay inside the private source directory")
        target = root.joinpath(*relative.parts)
        source = target.read_text(encoding="utf-8").splitlines(keepends=True)
        i += 2
        offset = 0
        while i < len(lines) and lines[i].startswith("@@ "):
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@", lines[i])
            if not match:
                raise ValueError("Invalid audio dependency patch hunk")
            at = int(match[1]) - 1 + offset
            i += 1
            old, new = [], []
            while i < len(lines) and lines[i][:1] in (" ", "+", "-") and not lines[i].startswith("--- a/"):
                line = lines[i]
                if line[0] in " -":
                    old.append(line[1:])
                if line[0] in " +":
                    new.append(line[1:])
                i += 1
            if source[at:at + len(old)] != old:
                matches = [start for start in range(len(source) - len(old) + 1)
                           if source[start:start + len(old)] == old]
                if len(matches) != 1:
                    raise ValueError(f"Unsupported HOE sources: audio contract patch does not match {relative}")
                at = matches[0]
            source[at:at + len(old)] = new
            offset += len(new) - len(old)
        target.write_text("".join(source), encoding="utf-8", newline="\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=pathlib.Path, required=True)
    parser.add_argument("--patch", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError("The audio dependency output must be separate from the source checkout")
    if not (source / "common/lwindex_utils.c").is_file():
        raise ValueError("The strict audio contract requires the HOE L-SMASH-Works variant")
    output.mkdir(parents=True, exist_ok=True)
    for directory in ("common", "xxHash"):
        shutil.copytree(source / directory, output / directory, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".git"))
    apply_patch(output, args.patch.resolve())


if __name__ == "__main__":
    main()
