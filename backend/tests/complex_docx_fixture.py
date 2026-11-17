"""Synthetic valid OOXML fixture. No user files, credentials or external calls."""
from io import BytesIO
import zipfile
from xml.etree import ElementTree as ET

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT


def complex_docx(*, variant="ORIGINAL", header_count=1):
    document = Document()
    paragraph = document.add_paragraph("Body original unchanged ")
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), paragraph.part.relate_to("https://docx-safety.invalid/<script>unsafe</script>", RT.HYPERLINK, is_external=True))
    run, text = OxmlElement("w:r"), OxmlElement("w:t")
    text.text = "VISIBLE_LINK"
    run.append(text); hyperlink.append(run); paragraph._p.append(hyperlink)
    for kind, ident in (("footnote", "4"), ("endnote", "7")):
        run, note = OxmlElement("w:r"), OxmlElement("w:" + kind + "Reference")
        note.set(qn("w:id"), ident); run.append(note); paragraph._p.append(run)
    table = document.add_table(rows=2, cols=3)
    table.cell(0, 0).merge(table.cell(0, 1)).text = "MERGED_HORIZONTAL"
    table.cell(0, 2).merge(table.cell(1, 2)).text = "MERGED_VERTICAL"
    nested = table.cell(1, 0).add_table(rows=1, cols=1)
    nested.cell(0, 0).text = "NESTED_TABLE " + variant
    table.cell(1, 1).text = "CELL_VALUE_42"
    document.add_paragraph("Body final paragraph")
    header = document.sections[0].header
    header.paragraphs[0].text = "HEADER_0 " + variant
    for index in range(1, header_count):
        header.add_paragraph(f"HEADER_{index} " + variant)
    document.sections[0].footer.paragraphs[0].text = "FOOTER " + variant
    # Keep a real drawing with author-supplied alt text. Pixels contain no text.
    from PIL import Image
    image = BytesIO(); Image.new("RGB", (4, 4), "blue").save(image, format="PNG"); image.seek(0)
    shape = document.add_paragraph().add_run().add_picture(image)
    shape._inline.docPr.set("descr", "AUTHOR_IMAGE_ALT " + variant)
    raw = BytesIO(); document.save(raw)
    with zipfile.ZipFile(BytesIO(raw.getvalue())) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    relns = "http://schemas.openxmlformats.org/package/2006/relationships"
    rels = ET.fromstring(members["word/_rels/document.xml.rels"])
    types = ET.fromstring(members["[Content_Types].xml"])
    for kind, ident in (("footnote", "4"), ("endnote", "7")):
        root = ET.Element("{" + ns + "}" + kind + "s")
        for nid, text in (("-1", "SEPARATOR_MUST_NOT_APPEAR"), (ident, kind.upper() + " " + variant), ("99", "UNREFERENCED_NOTE_MUST_NOT_APPEAR")):
            node = ET.SubElement(root, "{" + ns + "}" + kind, {"{" + ns + "}id": nid})
            if nid == "-1": node.set("{" + ns + "}type", "separator")
            p = ET.SubElement(node, "{" + ns + "}p"); r = ET.SubElement(p, "{" + ns + "}r")
            ET.SubElement(r, "{" + ns + "}t").text = text
        members[f"word/{kind}s.xml"] = ET.tostring(root)
        ET.SubElement(rels, "{" + relns + "}Relationship", {"Id": "rIdSynthetic" + kind,
            "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/" + kind + "s", "Target": kind + "s.xml"})
        ET.SubElement(types, "{http://schemas.openxmlformats.org/package/2006/content-types}Override", {
            "PartName": f"/word/{kind}s.xml", "ContentType": f"application/vnd.openxmlformats-officedocument.wordprocessingml.{kind}s+xml"})
    members["word/_rels/document.xml.rels"] = ET.tostring(rels)
    members["[Content_Types].xml"] = ET.tostring(types)
    # An unused header must not leak into parsing merely because it is in ZIP.
    members["word/unusedHeader.xml"] = f'<w:hdr xmlns:w="{ns}"><w:p><w:r><w:t>UNREFERENCED_HEADER_MUST_NOT_APPEAR</w:t></w:r></w:p></w:hdr>'.encode()
    result = BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items(): archive.writestr(name, data)
    return result.getvalue()
