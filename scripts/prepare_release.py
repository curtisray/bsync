"""Validate distributions and generate a checksummed Homebrew formula.

Run after `python -m build` using Python 3.10+. No network or third-party modules.
"""
import argparse
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import re
import tarfile
import zipfile


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def distributions(directory, tag):
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError("Use a stable release tag such as v0.2.1.")
    version = tag[1:]
    files = sorted([*directory.glob("*.whl"), *directory.glob("*.tar.gz")])
    expected = {f"better_rsync-{version}-py3-none-any.whl", f"better_rsync-{version}.tar.gz"}
    if {path.name for path in files} != expected:
        raise ValueError(f"Expected only {sorted(expected)} in {directory}. Use a clean output folder.")
    for path in files:
        if path.suffix == ".whl":
            with zipfile.ZipFile(path) as archive:
                metadata = archive.read(f"better_rsync-{version}.dist-info/METADATA")
        else:
            with tarfile.open(path) as archive:
                metadata = archive.extractfile(f"better_rsync-{version}/PKG-INFO").read()
        parsed = BytesParser().parsebytes(metadata)
        if parsed["Name"] != "better-rsync" or parsed["Version"] != version:
            raise ValueError(f"Package metadata doesn't match {tag}: {path}")
    return files


def formula(sdist, tag, resources):
    lines = [
        "class Bsync < Formula",
        "  include Language::Python::Virtualenv",
        "",
        '  desc "Rsync with live progress, guided setup, and reversible local transfers"',
        '  homepage "https://github.com/curtisray/bsync"',
        f'  url "https://github.com/curtisray/bsync/releases/download/{tag}/{sdist.name}"',
        f'  sha256 "{digest(sdist)}"',
        f'  version "{tag[1:]}"',
        '  license "MIT"',
        "",
        '  depends_on "python@3.14"',
        '  depends_on "rsync"',
    ]
    for resource in resources:
        lines += ["", f'  resource "{resource["name"]}" do',
                  f'    url "{resource["url"]}"', f'    sha256 "{resource["sha256"]}"', "  end"]
    lines += [
        "",
        "  def install",
        '    venv = virtualenv_create(libexec, "python3.14")',
        "    resources.each do |resource|",
        "      venv.pip_install resource, build_isolation: false",
        "    end",
        "    venv.pip_install_and_link buildpath, build_isolation: false",
        "  end",
        "",
        "  test do",
        '    assert_match version.to_s, shell_output("#{bin}/bsync --version")',
        '    ENV["BSYNC_DB"] = testpath/"history.db"',
        '    (testpath/"source.txt").write "bsync transfer test"',
        '    system bin/"bsync", "run", testpath/"source.txt", testpath/"destination.txt"',
        '    assert_equal "bsync transfer test", (testpath/"destination.txt").read',
        '    assert_match "COMPLETED", shell_output("#{bin}/bsync history")',
        "  end",
        "end",
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--verify", action="store_true", help="Verify downloaded artifacts against SHA256SUMS")
    args = parser.parse_args()
    files = distributions(args.dist, args.tag)
    sums = "".join(f"{digest(path)}  {path.name}\n" for path in files)
    if args.verify:
        if (args.dist / "SHA256SUMS").read_text() != sums:
            raise ValueError("Release checksums do not match the downloaded distributions.")
        print("Release metadata and SHA-256 checksums verified.")
        return
    resources = json.loads(Path(__file__).with_name("homebrew-resources.json").read_text())
    sdist = next(path for path in files if path.name.endswith(".tar.gz"))
    (args.dist / "bsync.rb").write_text(formula(sdist, args.tag, resources))
    (args.dist / "SHA256SUMS").write_text(sums)
    print("Prepared bsync.rb and SHA256SUMS alongside validated release distributions.")


if __name__ == "__main__":
    main()
