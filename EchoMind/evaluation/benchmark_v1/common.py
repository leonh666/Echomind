"""Standard-library IO shared by benchmark tools. Never imports the application."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CASE_FILES = ("intent_cases.jsonl", "rag_cases.jsonl", "contract_cases.jsonl")


def read_jsonl(path):
    rows = []
    with Path(path).open(encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as error:
                    raise ValueError(f"{path}:{line_number}: {error}") from error
    return rows


def write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n"
                            for row in rows), encoding="utf-8", newline="\n")


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8", newline="\n")


def load_cases(split="all"):
    rows = [row for filename in CASE_FILES for row in read_jsonl(DATA / filename)]
    return [row for row in rows if split == "all" or row["split"] == split]


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
