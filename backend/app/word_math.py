"""Convert LaTeX through MathML to editable Word Office Math, never a bitmap."""
from __future__ import annotations

from xml.etree import ElementTree as ET

from docx.oxml import OxmlElement
from docx.oxml.ns import qn


def element(name, *children):
    result = OxmlElement("m:" + name)
    for child in children:
        result.append(child)
    return result


def run(text):
    value = element("t")
    value.text = text or ""
    value.set(qn("xml:space"), "preserve")
    return element("r", value)


def _sequence(node):
    result = []
    if node.text and node.text.strip():
        result.append(run(node.text))
    for child in node:
        result.extend(convert(child))
        if child.tail and child.tail.strip():
            result.append(run(child.tail))
    return result


def convert(node):
    tag = node.tag.rsplit("}", 1)[-1]
    kids = list(node)
    if tag in {"math", "mrow", "mstyle", "mpadded", "semantics"}:
        return _sequence(node)
    if tag in {"annotation", "annotation-xml"}:
        return []
    if tag in {"mi", "mn", "mo", "mtext", "ms"}:
        return [run("".join(node.itertext()))]
    if tag == "mspace":
        return [run(" ")]
    def wrap(name, child):
        return element(name, *convert(child))
    if tag == "mfrac" and len(kids) == 2:
        return [element("f", wrap("num", kids[0]), wrap("den", kids[1]))]
    if tag in {"msup", "msub", "msubsup"}:
        expected = 3 if tag == "msubsup" else 2
        if len(kids) != expected:
            raise ValueError("invalid script")
        names = {"msup": ("sSup", ["e", "sup"]), "msub": ("sSub", ["e", "sub"]),
                 "msubsup": ("sSubSup", ["e", "sub", "sup"])}
        kind, labels = names[tag]
        return [element(kind, *(wrap(name, child) for name, child in zip(labels, kids))) ]
    if tag == "msqrt":
        hidden = element("degHide"); hidden.set(qn("m:val"), "1")
        return [element("rad", element("radPr", hidden), element("deg"), element("e", *_sequence(node)))]
    if tag == "mroot" and len(kids) == 2:
        return [element("rad", wrap("deg", kids[1]), wrap("e", kids[0]))]
    if tag in {"munder", "mover", "munderover"}:
        if len(kids) != (3 if tag == "munderover" else 2):
            raise ValueError("invalid limit")
        base = kids[0]
        if tag == "munderover" and "".join(base.itertext()) in {"∑", "∏", "∫", "∮", "⋃", "⋂"}:
            char = element("chr"); char.set(qn("m:val"), "".join(base.itertext()))
            loc = element("limLoc"); loc.set(qn("m:val"), "undOvr")
            return [element("nary", element("naryPr", char, loc), wrap("sub", kids[1]), wrap("sup", kids[2]), element("e"))]
        if tag == "munderover":
            lower = element("limLow", wrap("e", base), wrap("lim", kids[1]))
            return [element("limUpp", element("e", lower), wrap("lim", kids[2]))]
        if tag == "mover" and node.get("accent") == "true":
            char = element("chr"); char.set(qn("m:val"), "".join(kids[1].itertext()))
            return [element("acc", element("accPr", char), wrap("e", base))]
        return [element("limLow" if tag == "munder" else "limUpp", wrap("e", base), wrap("lim", kids[1]))]
    if tag == "mfenced":
        begin, end = element("begChr"), element("endChr")
        begin.set(qn("m:val"), node.get("open", "(")); end.set(qn("m:val"), node.get("close", ")"))
        return [element("d", element("dPr", begin, end), *(element("e", *convert(child)) for child in kids))]
    if tag == "mtable":
        rows = []
        for row in kids:
            if row.tag.rsplit("}", 1)[-1] not in {"mtr", "mlabeledtr"}:
                raise ValueError("invalid matrix")
            rows.append(element("mr", *(element("e", *_sequence(cell)) for cell in row)))
        return [element("m", *rows)]
    if tag == "menclose" and node.get("notation") in {"box", "roundedbox"}:
        return [element("borderBox", element("e", *_sequence(node)))]
    # Refuse lossy fallbacks such as phantom, unsupported spacing, enclosures,
    # or multi-scripts. The export explicitly preserves the original LaTeX.
    raise ValueError("unsupported MathML element: " + tag)


def latex_math(text):
    from latex2mathml.converter import convert as latex_to_mathml
    root = ET.fromstring(latex_to_mathml(text))
    return element("oMath", *convert(root))
