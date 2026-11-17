"""Local OOXML structure, without rendering, fetching links or interpreting pixels.

Body paragraphs/flattened rows remain the original v1 compatibility sequence.
Additional blocks use package part + XML path identities in the pinned revision.
"""
from __future__ import annotations

import hashlib
import io
import posixpath
import zipfile
from collections import defaultdict
from xml.etree import ElementTree as ET

from .source_store import SourceError

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
P = "{http://schemas.openxmlformats.org/package/2006/relationships}"
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
LABELS = {"body": "正文", "header": "页眉", "footer": "页脚", "footnote": "脚注", "endnote": "尾注"}


def xml_children(element):
    """One path numbering contract for extraction and raw-structure proofs."""
    counts = defaultdict(int)
    for child in element:
        local = child.tag.rsplit("}", 1)[-1]
        counts[local] += 1
        yield child, f"{local}[{counts[local]}]"


def table_elements(root, *, section_kind, note_ids=()):
    """Enumerate only supported table paths; never interpret caller XPath.

    The wrapper flag prevents a table behind extraction-sensitive content
    controls or a hidden ancestor row from being certified as a complete
    ordinary rectangle. It does not alter extraction or path numbering.
    """
    def content(element, path, wrapped=False):
        for child, suffix in xml_children(element):
            child_path = path + "/" + suffix
            if child.tag == W + "tbl":
                yield from table(child, child_path, wrapped)
            elif child.tag == W + "sdt":
                nested = child.find(W + "sdtContent")
                if nested is not None:
                    yield from content(nested, child_path + "/sdtContent", True)

    def table(element, path, wrapped=False, depth=0):
        yield path, element, wrapped
        if depth >= 32:
            return
        for row_index, row in enumerate(element.findall(W + "tr"), 1):
            descendant_wrapped = wrapped or row.find(W + "trPr/" + W + "hidden") is not None
            for cell_index, cell in enumerate(row.findall(W + "tc"), 1):
                for child, suffix in xml_children(cell):
                    if child.tag == W + "tbl":
                        yield from table(child,
                            f"{path}/tr[{row_index}]/tc[{cell_index}]/" + suffix, descendant_wrapped, depth + 1)

    if section_kind == "body" and root.tag == W + "document":
        bodies = root.findall(W + "body")
        if len(bodies) == 1:
            yield from content(bodies[0], "/document/body")
    elif section_kind in ("header", "footer") and root.tag == W + {"header": "hdr", "footer": "ftr"}[section_kind]:
        yield from content(root, "/" + section_kind)
    elif section_kind in ("footnote", "endnote") and root.tag == W + section_kind + "s":
        for note in root.findall(W + section_kind):
            ident = note.get(W + "id")
            if ident in note_ids and note.get(W + "type", "normal") == "normal":
                yield from content(note, f"/{section_kind}s/{section_kind}[@id={ident}]")


def body_blocks(document):
    """Exactly the v1 order and visible text, including empty paragraphs."""
    from docx.text.paragraph import Paragraph
    for item in document.iter_inner_content():
        if isinstance(item, Paragraph):
            yield {"kind": "word_paragraph", "text": item.text}
        else:
            for row in item.rows:
                yield {"kind": "word_table_row", "text": "\t".join(cell.text for cell in row.cells),
                       "retrieval_role": "compatibility"}


def supplement(raw, legacy, add):
    archive = zipfile.ZipFile(io.BytesIO(raw))
    names = set(archive.namelist())
    cache, relationships, image_hashes = {}, {}, {}
    metadata = {"part_count": 0, "supplemental_block_count": 0,
                "page_layout_available": False, "image_understanding_performed": False,
                "ocr_performed": False, "external_requests": 0}
    omissions = {"rendered_page_layout", "image_understanding", "chart_and_embedded_object_interpretation",
                 "tracked_change_acceptance", "textbox_text_and_layout", "equation_markup", "comments_and_revision_history"}
    required_notes = {"footnote": set(), "endnote": set()}
    emitted, visited_parts = set(), set()

    def xml(part):
        if part not in cache:
            if part not in names:
                raise SourceError("Word 引用的内容部件缺失，不能宣称完整读取")
            cache[part] = ET.fromstring(archive.read(part))
        return cache[part]

    def rels(part):
        if part in relationships:
            return relationships[part]
        rel_part = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
        result = {}
        if rel_part in names:
            for item in xml(rel_part):
                ident, target = item.get("Id"), item.get("Target", "")
                if not ident or ident in result:
                    raise SourceError("Word 关系标识重复或缺失")
                external = item.get("TargetMode") == "External"
                resolved = None
                if not external:
                    if "\\" in target or "\0" in target or ":" in target or target.startswith("//"):
                        raise SourceError("Word 内容部件路径无效")
                    resolved = posixpath.normpath(target.lstrip("/") if target.startswith("/")
                        else posixpath.join(posixpath.dirname(part), target))
                    if resolved.startswith("../") or resolved == ".." or resolved.startswith("/"):
                        raise SourceError("Word 内容部件超出原件范围")
                result[ident] = {"type": item.get("Type", "").rsplit("/", 1)[-1],
                                 "target": target, "part": resolved, "external": external}
        relationships[part] = result
        return result

    def texts(element):
        # Deliberately exclude deleted text, fields' instruction code and drawing
        # textboxes; tracked revisions are not silently accepted as a final view.
        pieces = []
        def walk(node):
            if node.tag in (W + "del", W + "txbxContent", W + "instrText"):
                return
            if node.tag == W + "t":
                pieces.append(node.text or "")
            elif node.tag == W + "tab":
                pieces.append("\t")
            elif node.tag in (W + "br", W + "cr"):
                pieces.append("\n")
            else:
                for child in node:
                    walk(child)
        walk(element)
        return "".join(pieces)

    def inline(element, part):
        links, images = [], []
        for node in element.iter():
            if node.tag == W + "hyperlink":
                relation = rels(part).get(node.get(R + "id"))
                links.append({"text": texts(node), "target": relation["target"] if relation else None,
                              "anchor": node.get(W + "anchor"), "external": bool(relation and relation["external"]),
                              "visited": False})
            if node.tag in (W + "footnoteReference", W + "endnoteReference"):
                kind = "footnote" if node.tag == W + "footnoteReference" else "endnote"
                required_notes[kind].add(node.get(W + "id"))
            if node.tag == W + "drawing":
                props = list(node.iter(WP + "docPr"))
                blips = list(node.iter(A + "blip"))
                for i, prop in enumerate(props):
                    blip = blips[min(i, len(blips) - 1)] if blips else None
                    relation = rels(part).get(blip.get(R + "embed") or blip.get(R + "link")) if blip is not None else None
                    image_part = relation.get("part") if relation else None
                    if image_part in names and image_part not in image_hashes:
                        image_hashes[image_part] = hashlib.sha256(archive.read(image_part)).hexdigest()
                    image_hash = image_hashes.get(image_part)
                    images.append({"title": prop.get("title", ""), "description": prop.get("descr", ""),
                                   "image_part": image_part, "image_sha256": image_hash,
                                   "external": bool(relation and relation["external"]),
                                   "pixels_understood": False, "ocr_performed": False})
        return links, images

    def emit(element, part, path, section, **extra):
        key = part + "#" + path
        if key in emitted:
            return
        emitted.add(key)
        links, images = inline(element, part)
        text = texts(element)
        descriptions = [s for image in images for s in (image["title"], image["description"]) if s]
        # These are author-supplied metadata strings, never generated OCR.
        if descriptions:
            text += ("\n" if text else "") + "\n".join(descriptions)
        add({"kind": "word_part", "block_id": "wb_" + hashlib.sha256(key.encode()).hexdigest()[:32],
             "part_name": part, "xml_path": path, "section_kind": section, "part_label": LABELS[section],
             "text": text, "hyperlinks": links, "images": images, **extra})
        metadata["supplemental_block_count"] += 1

    def table(element, part, path, section, depth=0):
        if depth > 32:
            raise SourceError("Word 嵌套表格超过32层，请拆分原件")
        for row_index, row in enumerate(element.findall(W + "tr"), 1):
            before = row.find(W + "trPr/" + W + "gridBefore")
            column = int(before.get(W + "val", "0")) + 1 if before is not None else 1
            for cell_index, cell in enumerate(row.findall(W + "tc"), 1):
                props = cell.find(W + "tcPr")
                span_node = props.find(W + "gridSpan") if props is not None else None
                span = int(span_node.get(W + "val", "1")) if span_node is not None else 1
                if not 1 <= span <= 512 or column > 512:
                    raise SourceError("Word 表格逻辑列超出解析限制")
                merge = props.find(W + "vMerge") if props is not None else None
                cell_path = f"{path}/tr[{row_index}]/tc[{cell_index}]"
                for child, suffix in xml_children(cell):
                    if child.tag == W + "p":
                        emit(child, part, cell_path + "/" + suffix, section, table_path=path,
                             table_row=row_index, table_column=column, column_span=span,
                             vertical_merge=merge.get(W + "val", "continue") if merge is not None else None,
                             table_depth=depth, logical_grid=True)
                    elif child.tag == W + "tbl":
                        table(child, part, cell_path + "/" + suffix, section, depth + 1)
                column += span

    def content(element, part, path, section, *, body=False):
        visited_parts.add(part)
        for child, suffix in xml_children(element):
            child_path = path + "/" + suffix
            if child.tag == W + "p":
                if body:
                    # Metadata belongs to the old body paragraph; extra author
                    # image descriptions need independently selectable blocks.
                    links, images = inline(child, part)
                    index = next(body_indexes)
                    legacy[index].update(part_name=part, xml_path=child_path, hyperlinks=links, images=images)
                    if images:
                        clone = ET.Element(W + "p")
                        for drawing in child.iter(W + "drawing"):
                            clone.append(drawing)
                        emit(clone, part, child_path + "/image_metadata", section, content_role="image_metadata")
                else:
                    emit(child, part, child_path, section)
            elif child.tag == W + "tbl":
                table(child, part, child_path, section)
            elif child.tag == W + "sdt":
                nested = child.find(W + "sdtContent")
                if nested is not None:
                    content(nested, part, child_path + "/sdtContent", section)

    try:
        main = "word/document.xml"
        root = xml(main)
        body = root.find(W + "body")
        if body is None:
            raise SourceError("Word 正文部件缺失")
        # Paragraph index in compatibility sequence also counts table rows.
        body_indexes = iter(i for i, block in enumerate(legacy) if block["kind"] == "word_paragraph")
        content(body, main, "/document/body", "body", body=True)
        targets = []
        for kind in ("header", "footer"):
            for ref in root.iter(W + kind + "Reference"):
                relation = rels(main).get(ref.get(R + "id"))
                if not relation or relation["type"] != kind or relation["external"]:
                    raise SourceError("Word 页眉页脚关系无效")
                if (kind, relation["part"]) not in targets:
                    targets.append((kind, relation["part"]))
        for kind, part in targets:
            content(xml(part), part, "/" + kind, kind)
        for kind in ("footnote", "endnote"):
            ids = required_notes[kind]
            if not ids:
                continue
            relation = next((r for r in rels(main).values() if r["type"] == kind + "s" and not r["external"]), None)
            if relation is None:
                raise SourceError("Word 脚注或尾注内容部件缺失")
            part = relation["part"]
            found = set()
            for note in xml(part).findall(W + kind):
                ident = note.get(W + "id")
                if ident in ids and note.get(W + "type", "normal") == "normal":
                    content(note, part, f"/{kind}s/{kind}[@id={ident}]", kind)
                    found.add(ident)
            if found != ids:
                raise SourceError("Word 引用的脚注或尾注缺失")
        metadata["part_count"] = len(visited_parts)
        return metadata, sorted(omissions)
    finally:
        archive.close()
