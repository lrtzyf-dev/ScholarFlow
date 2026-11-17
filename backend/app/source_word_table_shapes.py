"""Bound one cited Word table's complete shape to retained OOXML bytes.

This is a structural proof only. It neither renders Word nor reads pixels,
follows external relationships, or treats author text as instructions.
"""
from __future__ import annotations

import hashlib
import io
import posixpath
import re
import zipfile
from xml.etree import ElementTree as ET

from .docx_extraction import W, R, P, table_elements
from . import source_store as store


PROTOCOL = "word_table_shape_v1"
SHAPE_KIND = "ooxml_rectangular_unmerged"
MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _part(value):
    return (isinstance(value, str) and len(value) <= 512 and value.startswith("word/")
        and value.endswith(".xml") and "\\" not in value and ":" not in value
        and "\0" not in value and posixpath.normpath(value) == value)


def _referenced_sections(archive, root, requested_part):
    """Only parts reachable by retained internal Word content references."""
    if requested_part == "word/document.xml":
        return [("body", ())]
    rel_path = "word/_rels/document.xml.rels"
    if rel_path not in archive.namelist():
        return []
    relationships = ET.fromstring(archive.read(rel_path))
    if relationships.tag != P + "Relationships":
        return []
    by_id = {}
    for relation in relationships:
        ident = relation.get("Id")
        if relation.tag != P + "Relationship" or not ident or ident in by_id:
            return []
        by_id[ident] = relation
    result = []
    def matches(relation, kind):
        if relation is None or relation.get("TargetMode", "Internal") != "Internal":
            return False
        if relation.get("Type") != "http://schemas.openxmlformats.org/officeDocument/2006/relationships/" + kind:
            return False
        target = relation.get("Target", "")
        if not target or any(s in target for s in ("\\", ":", "\0", "?", "#")) or target.startswith("//"):
            return False
        resolved = posixpath.normpath(target.lstrip("/") if target.startswith("/")
            else posixpath.join("word", target))
        return _part(resolved) and resolved == requested_part
    for kind in ("header", "footer"):
        if any(matches(by_id.get(node.get(R + "id")), kind) for node in root.iter(W + kind + "Reference")):
            result.append((kind, ()))
    for kind in ("footnote", "endnote"):
        ids = {node.get(W + "id") for node in root.iter(W + kind + "Reference")}
        if ids and all(isinstance(i, str) and re.fullmatch(r"-?\d{1,10}", i) for i in ids):
            if any(matches(relation, kind + "s") for relation in by_id.values()):
                result.append((kind, ids))
    return result


def _value(nodes, *, default, minimum=0):
    if not nodes:
        return default
    if len(nodes) != 1 or len(nodes[0]) or set(nodes[0].attrib) != {W + "val"}:
        return None
    raw = nodes[0].get(W + "val")
    if not isinstance(raw, str) or not re.fullmatch(r"\d{1,6}", raw):
        return None
    number = int(raw)
    return number if minimum <= number <= 512 else None


def _shape(table):
    """Fail closed on merges, omitted grid slots and unsupported wrappers."""
    tags = [child.tag for child in table]
    if any(tag not in {W + "tblPr", W + "tblGrid", W + "tr"} for tag in tags):
        return None, "wrapped_or_unsupported_table_children"
    grids, rows = table.findall(W + "tblGrid"), table.findall(W + "tr")
    if len(table.findall(W + "tblPr")) > 1 or len(grids) != 1:
        return None, "missing_or_ambiguous_grid"
    grid = grids[0]
    columns = list(grid)
    if (not 1 <= len(columns) <= 512 or any(c.tag != W + "gridCol" or len(c) for c in columns)
            or not 1 <= len(rows) <= 100_000):
        return None, "invalid_grid_or_row_extent"
    if tags != ([W + "tblPr"] if W + "tblPr" in tags else []) + [W + "tblGrid"] + [W + "tr"] * len(rows):
        return None, "noncanonical_table_structure"
    # Do not recursively consume nested tables as rows of this table. Changes
    # to the current structural level are uncertain; nested shapes are separate.
    if table.find(W + "tblPr/" + W + "tblPrChange") is not None:
        return None, "tracked_table_properties"
    for row in rows:
        if (any(c.tag not in {W + "trPr", W + "tc"} for c in row)
                or len(row.findall(W + "trPr")) > 1):
            return None, "wrapped_or_unsupported_row_children"
        props = row.find(W + "trPr")
        if props is not None:
            # A raw hidden row is not a certified ordinary table extent. Do
            # not turn display-dependent structure into a simple shape proof.
            if props.findall(W + "hidden"):
                return None, "hidden_row_structure"
            if any(c.tag in {W + "trPrChange", W + "ins", W + "del"} for c in props):
                return None, "tracked_row_structure"
            if any(_value(props.findall(W + field), default=0) != 0 for field in ("gridBefore", "gridAfter")):
                return None, "omitted_or_invalid_grid_slots"
        cells = row.findall(W + "tc")
        if len(cells) != len(columns):
            return None, "nonrectangular_grid"
        for cell in cells:
            if (any(c.tag not in {W + "tcPr", W + "p", W + "tbl"} for c in cell)
                    or len(cell.findall(W + "tcPr")) > 1 or not cell.findall(W + "p")):
                return None, "wrapped_or_unsupported_cell_children"
            props = cell.find(W + "tcPr")
            if props is not None:
                if any(props.findall(W + name) for name in ("vMerge", "hMerge", "tcPrChange", "cellMerge", "cellIns", "cellDel")):
                    return None, "merged_or_tracked_cells"
                if _value(props.findall(W + "gridSpan"), default=1, minimum=1) != 1:
                    return None, "merged_or_invalid_span"
    return {"row_count": len(rows), "column_count": len(columns)}, None


def verified_word_table_shape(db, revision_id, *, scope, part_name, table_path):
    """Return only the requested cited table's revision-bound strict metadata.

    Authorization and raw corruption errors propagate. An unsupported or
    unproven structure returns unknown without row/column dimensions. Older
    extraction caches are neither consulted nor modified.
    """
    record = store.read_revision(db, revision_id, scope=scope, include_raw=True)
    raw = record["raw_bytes"]
    if hashlib.sha256(raw).hexdigest() != record["raw_sha256"]:
        raise store.SourceCorruption("Word 固定版本原件校验失败")
    result = {"protocol": PROTOCOL, "revision_id": record["revision_id"], "raw_sha256": record["raw_sha256"],
        "part_name": part_name, "table_path": table_path, "complete": False, "shape_kind": "unknown",
        "basis": "retained_ooxml_direct_grid", "authorizes_actions": False}
    def unknown(reason):
        return {**result, "reason": reason}
    if record["media_type"] != MEDIA:
        return unknown("not_a_word_ooxml_revision")
    if not _part(part_name) or not isinstance(table_path, str) or not table_path.startswith("/") or len(table_path) > 4096:
        return unknown("invalid_table_identity")
    from .source_parsing import _office_zip
    try:
        _office_zip(raw)  # Bounded XML validation; rejects DTD/entities, never pixels.
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if "word/document.xml" not in archive.namelist():
                return unknown("missing_main_document")
            main = ET.fromstring(archive.read("word/document.xml"))
            if main.tag != W + "document":
                return unknown("invalid_main_document")
            sections = _referenced_sections(archive, main, part_name)
            if not sections or part_name not in archive.namelist():
                return unknown("part_not_referenced_or_missing")
            root = main if part_name == "word/document.xml" else ET.fromstring(archive.read(part_name))
            matches = [(element, wrapped) for kind, ids in sections
                for path, element, wrapped in table_elements(root, section_kind=kind, note_ids=ids)
                if path == table_path]
            if len(matches) != 1:
                return unknown("table_path_not_unique_or_supported")
            element, wrapped = matches[0]
            if wrapped:
                return unknown("wrapped_table_structure")
            shape, reason = _shape(element)
            if shape is None:
                return unknown(reason)
            return {**result, "complete": True, "shape_kind": SHAPE_KIND, **shape}
    except (store.SourceError, ET.ParseError, zipfile.BadZipFile, ValueError, OSError, RuntimeError):
        return unknown("unsupported_or_invalid_ooxml")
