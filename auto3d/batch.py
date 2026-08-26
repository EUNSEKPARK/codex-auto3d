"""Batch input parsing: .txt (one concept per line), .csv, .json, .xlsx (stdlib zip+xml reader).

Every row becomes a BatchItem. Recognised columns (case-insensitive, Korean aliases accepted):
concept/prompt/개념/프롬프트, name/이름, profile/프로파일, quality/품질, views/뷰, complexity/복잡도, style/스타일.
"""

from __future__ import annotations

import csv
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .util import Auto3DError

COLUMN_ALIASES = {
    "concept": {"concept", "prompt", "subject", "text", "개념", "프롬프트", "컨셉", "설명", "주제", "대상"},
    "name": {"name", "title", "id", "이름", "제목"},
    "profile": {"profile", "프로파일", "유형"},
    "quality": {"quality", "품질", "타깃", "target"},
    "views": {"views", "view", "뷰", "추가뷰"},
    "complexity": {"complexity", "복잡도"},
    "style": {"style", "스타일"},
}


@dataclass
class BatchItem:
    concept: str
    name: str | None = None
    overrides: dict[str, Any] = field(default_factory=dict)
    source_row: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"concept": self.concept, "name": self.name, "overrides": self.overrides, "row": self.source_row}


def _normalise_header(header: list[str]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for index, raw in enumerate(header):
        key = (raw or "").strip().lower()
        for canonical, aliases in COLUMN_ALIASES.items():
            if key in aliases:
                mapping[index] = canonical
                break
    return mapping


def _rows_to_items(rows: list[list[str]]) -> list[BatchItem]:
    if not rows:
        return []
    header_map = _normalise_header(rows[0])
    items: list[BatchItem] = []
    if "concept" in header_map.values():
        for number, row in enumerate(rows[1:], start=2):
            if not any((cell or "").strip() for cell in row):
                continue
            record: dict[str, str] = {}
            for index, key in header_map.items():
                if index < len(row) and (row[index] or "").strip():
                    record[key] = row[index].strip()
            if not record.get("concept"):
                continue
            items.append(_item_from_record(record, number))
    else:
        # headerless: first column is the concept, optional second column the name
        for number, row in enumerate(rows, start=1):
            if not row or not (row[0] or "").strip():
                continue
            if (row[0] or "").strip().startswith("#"):
                continue
            record = {"concept": row[0].strip()}
            if len(row) > 1 and (row[1] or "").strip():
                record["name"] = row[1].strip()
            items.append(_item_from_record(record, number))
    return items


def _item_from_record(record: dict[str, Any], number: int) -> BatchItem:
    overrides: dict[str, Any] = {}
    for key in ("profile", "quality", "complexity", "style"):
        if record.get(key):
            overrides[key] = str(record[key]).strip()
    if record.get("views"):
        raw = record["views"]
        overrides["views"] = [part.strip() for part in re.split(r"[,;/ ]+", str(raw)) if part.strip()]
    return BatchItem(concept=str(record["concept"]).strip(), name=(str(record["name"]).strip() if record.get("name") else None), overrides=overrides, source_row=number)


def parse_txt(path: Path) -> list[BatchItem]:
    items: list[BatchItem] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        name = None
        if "|" in text:
            concept, name = (part.strip() for part in text.split("|", 1))
        else:
            concept = text
        items.append(BatchItem(concept=concept, name=name or None, source_row=number))
    return items


def parse_csv(path: Path) -> list[BatchItem]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [list(row) for row in csv.reader(handle)]
    return _rows_to_items(rows)


def parse_json(path: Path) -> list[BatchItem]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("items") or data.get("prompts") or data.get("jobs") or []
    if not isinstance(data, list):
        raise Auto3DError("batch JSON must be a list (or an object with an items/prompts/jobs list)")
    items: list[BatchItem] = []
    for number, entry in enumerate(data, start=1):
        if isinstance(entry, str):
            items.append(BatchItem(concept=entry.strip(), source_row=number))
        elif isinstance(entry, dict):
            record = {key: value for key, value in entry.items() if value not in (None, "")}
            for canonical, aliases in COLUMN_ALIASES.items():
                for alias in aliases:
                    if alias in record and canonical not in record:
                        record[canonical] = record.pop(alias)
            if not record.get("concept"):
                continue
            if isinstance(record.get("views"), list):
                record["views"] = ",".join(str(v) for v in record["views"])
            items.append(_item_from_record(record, number))
    return items


# ---------------------------------------------------------------------------
# minimal xlsx reader (first worksheet, shared strings + inline strings + numbers)
# ---------------------------------------------------------------------------

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _column_index(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference or "")
    if not letters:
        return 0
    value = 0
    for char in letters.group(0):
        value = value * 26 + (ord(char) - 64)
    return value - 1


def parse_xlsx(path: Path) -> list[BatchItem]:
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise Auto3DError(f"not a valid .xlsx file: {path}") from exc
    with archive:
        names = archive.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")))
        sheet_name = next((name for name in sorted(names) if re.match(r"xl/worksheets/sheet\d+\.xml$", name)), None)
        if sheet_name is None:
            raise Auto3DError(f"no worksheet found in {path}")
        root = ET.fromstring(archive.read(sheet_name))
        rows: list[list[str]] = []
        for row in root.iter(f"{{{_NS['m']}}}row"):
            cells: dict[int, str] = {}
            for cell in row.findall("m:c", _NS):
                index = _column_index(cell.get("r", ""))
                kind = cell.get("t")
                value = ""
                if kind == "s":
                    v = cell.find("m:v", _NS)
                    if v is not None and v.text and v.text.isdigit() and int(v.text) < len(shared):
                        value = shared[int(v.text)]
                elif kind == "inlineStr":
                    value = "".join(t.text or "" for t in cell.iter(f"{{{_NS['m']}}}t"))
                else:
                    v = cell.find("m:v", _NS)
                    value = (v.text or "") if v is not None else ""
                cells[index] = value
            if cells:
                width = max(cells) + 1
                rows.append([cells.get(i, "") for i in range(width)])
            else:
                rows.append([])
    return _rows_to_items(rows)


def parse_batch_file(path: Path) -> list[BatchItem]:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise Auto3DError(f"batch file not found: {path}")
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        items = parse_txt(path)
    elif suffix == ".csv":
        items = parse_csv(path)
    elif suffix == ".json":
        items = parse_json(path)
    elif suffix in {".xlsx", ".xlsm"}:
        items = parse_xlsx(path)
    else:
        raise Auto3DError(f"unsupported batch file type: {suffix} (use .txt, .csv, .json or .xlsx)")
    if not items:
        raise Auto3DError(f"no concepts found in {path}")
    return items
