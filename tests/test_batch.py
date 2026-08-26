from __future__ import annotations

import json
import shutil
import unittest
import zipfile
from pathlib import Path

from support import temp_dir  # noqa: E402

from auto3d.batch import parse_batch_file  # noqa: E402
from auto3d.util import Auto3DError  # noqa: E402


def _write_xlsx(path: Path, rows: list[list[str]]) -> None:
    """Minimal xlsx with inline strings (what the reader must handle besides shared strings)."""

    def col(index: int) -> str:
        letters = ""
        index += 1
        while index:
            index, rem = divmod(index - 1, 26)
            letters = chr(65 + rem) + letters
        return letters

    sheet_rows = []
    for r, row in enumerate(rows, start=1):
        cells = "".join(
            f'<c r="{col(c)}{r}" t="inlineStr"><is><t>{value}</t></is></c>' for c, value in enumerate(row) if value != ""
        )
        sheet_rows.append(f'<row r="{r}">{cells}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(sheet_rows)}</sheetData></worksheet>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"></Types>')
        archive.writestr("xl/workbook.xml", '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets><sheet name="S" sheetId="1" r:id="rId1" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/></sheets></workbook>')
        archive.writestr("xl/worksheets/sheet1.xml", sheet)


class BatchParsingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir()

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_txt_lines_with_optional_names(self) -> None:
        path = self.dir / "p.txt"
        path.write_text("# comment\n빨간 장난감 로봇 | robot\n\n파란 물뿌리개\n", encoding="utf-8")
        items = parse_batch_file(path)
        self.assertEqual([i.concept for i in items], ["빨간 장난감 로봇", "파란 물뿌리개"])
        self.assertEqual(items[0].name, "robot")
        self.assertIsNone(items[1].name)

    def test_csv_with_korean_header(self) -> None:
        path = self.dir / "p.csv"
        path.write_text("개념,이름,프로파일,품질,뷰\n나무 의자,chair,generic,draft,\"front, side\"\n캐릭터 소녀,,character,full,\n", encoding="utf-8-sig")
        items = parse_batch_file(path)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].overrides["views"], ["front", "side"])
        self.assertEqual(items[0].overrides["quality"], "draft")
        self.assertEqual(items[1].overrides["profile"], "character")
        self.assertIsNone(items[1].name)

    def test_csv_headerless(self) -> None:
        path = self.dir / "p.csv"
        path.write_text("a red mug\na green bottle,bottle\n", encoding="utf-8")
        items = parse_batch_file(path)
        self.assertEqual([i.concept for i in items], ["a red mug", "a green bottle"])
        self.assertEqual(items[1].name, "bottle")

    def test_json_objects_and_strings(self) -> None:
        path = self.dir / "p.json"
        path.write_text(json.dumps([{"concept": "x", "views": ["front"]}, "plain string"]), encoding="utf-8")
        items = parse_batch_file(path)
        self.assertEqual(items[0].overrides["views"], ["front"])
        self.assertEqual(items[1].concept, "plain string")

    def test_xlsx_inline_strings(self) -> None:
        path = self.dir / "p.xlsx"
        _write_xlsx(path, [["concept", "name", "quality"], ["golden trophy", "trophy", "standard"], ["", "", ""], ["wooden toy train", "", "draft"]])
        items = parse_batch_file(path)
        self.assertEqual([i.concept for i in items], ["golden trophy", "wooden toy train"])
        self.assertEqual(items[0].overrides["quality"], "standard")

    def test_unknown_extension(self) -> None:
        path = self.dir / "p.docx"
        path.write_bytes(b"nope")
        with self.assertRaises(Auto3DError):
            parse_batch_file(path)

    def test_empty_file_is_an_error(self) -> None:
        path = self.dir / "p.txt"
        path.write_text("# only a comment\n", encoding="utf-8")
        with self.assertRaises(Auto3DError):
            parse_batch_file(path)


if __name__ == "__main__":
    unittest.main()
