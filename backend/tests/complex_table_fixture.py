"""Synthetic golden workbook, with deliberately stale and missing Excel caches."""
from datetime import datetime
from io import BytesIO
import zipfile
from xml.etree import ElementTree as ET

from openpyxl import Workbook


def workbook_bytes():
    book = Workbook()
    sheet = book.active
    sheet.title = "实验 数据"
    sheet.append(["组", "剂量 (mg)", "比例 (%)", "日期", "公式", "缺缓存", "复杂公式", "合并", "合并"])
    sheet.append(["A", 2, 0.25, datetime(2020, 2, 29), "=SUM(B2,'对照'!A2)", "=B2*2", '=INDIRECT("B2")', "唯一锚点"])
    sheet.append(["B", None, 0.125, datetime(2021, 1, 1), "=B2+1", "=B3+1", "=E2*2"])
    sheet["C2"].number_format = "0.0%"
    sheet["D2"].number_format = "yyyy-mm-dd"
    sheet.merge_cells("H2:I3")
    sheet.row_dimensions[3].hidden = True
    other = book.create_sheet("对照")
    other.append(["Value"])
    other.append([3])
    other.sheet_state = "hidden"
    output = BytesIO()
    book.save(output); book.close()
    # openpyxl deliberately never calculates formulas; patch synthetic cached
    # values into OOXML so tests can distinguish them from real recomputation.
    patched = BytesIO()
    with zipfile.ZipFile(output) as source, zipfile.ZipFile(patched, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            raw = source.read(item)
            if item.filename == "xl/worksheets/sheet1.xml":
                root = ET.fromstring(raw)
                ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                for cell in root.findall("m:sheetData/m:row/m:c", ns):
                    cache = {"E2": "999", "E3": "3", "G3": "10"}.get(cell.get("r"))
                    if cache is not None:
                        cell.find("m:v", ns).text = cache
                raw = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            target.writestr(item, raw)
    return patched.getvalue()
