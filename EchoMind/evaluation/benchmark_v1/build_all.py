"""Deterministically rebuild synthetic fixtures and exports, then validate them.

These scripts own the generated fixtures. Keep independent human reviews in
separate annotation files so a rebuild never overwrites review evidence.
"""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def main():
    for script, arguments in (("build_corpus.py", []), ("build_cases.py", []),
                              ("export.py", []), ("validate.py", ["--write-manifest"])):
        subprocess.run([sys.executable, str(ROOT / script), *arguments], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
