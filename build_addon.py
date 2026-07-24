#!/usr/bin/env python3
"""Package the add-on into dist/anki-voice-review.ankiaddon.

An .ankiaddon is a plain zip whose *contents* sit at the archive root (no top-level folder),
with a manifest.json naming the package. Anki installs it via Tools → Add-ons → Install from
file.

The shared `avr/` modules are vendored into the archive rather than imported from site-packages,
because an add-on only gets Anki's own interpreter and cannot see this project's virtualenv.

Before packaging, every vendored module is byte-compiled against **Python 3.9** — the version
Anki 25.x bundles. This is the guard that stops 3.10+ syntax (`X | Y` at runtime, `match`)
reaching the add-on, where it would only surface as a load failure on the user's machine.
"""

from __future__ import annotations

import json
import py_compile
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
ADDON_DIR = ROOT / "addon"
DIST = ROOT / "dist"
OUTPUT = DIST / "anki-voice-review.ankiaddon"

PACKAGE = "anki_voice_review"
NAME = "Voice Review"

# Modules shared with the CLI. `anki.py` (the AnkiConnect client), `runner.py`, and `cli.py`
# are deliberately excluded: inside Anki the add-on talks to the reviewer directly.
VENDORED = [
    "__init__.py",
    "cards.py",
    "config.py",
    "diagnostics.py",
    "grade.py",
    "session.py",
    "stt.py",
    "tts.py",
]

ANKI_PYTHON = "3.9"


def check_python39_syntax(files: list[Path]) -> None:
    """Fail the build if anything would not parse under Anki's interpreter."""
    interpreter = shutil.which(f"python{ANKI_PYTHON}")
    if interpreter:
        for path in files:
            result = subprocess.run(
                [interpreter, "-m", "py_compile", str(path)], capture_output=True, text=True
            )
            if result.returncode:
                raise SystemExit(
                    f"{path.name} is not valid Python {ANKI_PYTHON} "
                    f"(the version Anki bundles):\n{result.stderr}"
                )
        print(f"  syntax checked against python{ANKI_PYTHON}")
        return

    # No 3.9 interpreter here. Compile with the running one so at least gross syntax errors are
    # caught, and say plainly that the version-specific check did not happen.
    for path in files:
        py_compile.compile(str(path), doraise=True)
    print(
        f"  warning: python{ANKI_PYTHON} not found, so only checked against "
        f"{sys.version_info.major}.{sys.version_info.minor}. "
        f"Install it (uv python install {ANKI_PYTHON}) for the real check."
    )


def build() -> Path:
    print(f"Building {OUTPUT.name}")
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / PACKAGE
        stage.mkdir(parents=True)

        for item in ADDON_DIR.iterdir():
            if item.name == "__pycache__":
                continue
            (shutil.copytree if item.is_dir() else shutil.copy2)(item, stage / item.name)

        vendor = stage / "avr"
        vendor.mkdir()
        for filename in VENDORED:
            source = ROOT / "avr" / filename
            if not source.exists():
                if filename == "__init__.py":
                    (vendor / filename).write_text('"""Vendored shared logic."""\n')
                    continue
                raise SystemExit(f"missing module to vendor: {source}")
            shutil.copy2(source, vendor / filename)
        print(f"  vendored {len(VENDORED)} shared module(s)")

        (stage / "manifest.json").write_text(
            json.dumps(
                {
                    "package": PACKAGE,
                    "name": NAME,
                    "mod": 0,
                    "conflicts": [],
                    "min_point_version": 50,
                    "human_version": "0.1.0",
                },
                indent=2,
            )
            + "\n"
        )

        check_python39_syntax(sorted(stage.rglob("*.py")))

        DIST.mkdir(exist_ok=True)
        if OUTPUT.exists():
            OUTPUT.unlink()

        count = 0
        with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_dir() or "__pycache__" in path.parts:
                    continue
                # Contents at the archive root — Anki rejects a wrapping top-level folder.
                archive.write(path, path.relative_to(stage))
                count += 1
        print(f"  wrote {count} files -> {OUTPUT}")

    return OUTPUT


if __name__ == "__main__":
    path = build()
    print(f"\nInstall: Anki → Tools → Add-ons → Install from file… → {path}")
