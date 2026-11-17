"""Add already-read complete small-table context to exact prose citations.

No source interpretation, scientific verdict, new evidence or model call occurs
here. All proposed annotations still require the ordinary full reviews.
"""
from copy import deepcopy
import hashlib
import json
import re

from ..rag.citation import (MARKER_RE, SENT_RE, HEADING_RE, LIST_ITEM_RE,
    citation_gate, parse_claims, prose_for_citations)

PROTOCOL = 'fixed_small_table_context_v1'
MAX_CELLS = 16
MAX_ADDED = 8
MAX_ANSWER = 200_000


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _evidence_valid(evidence):
    return (isinstance(evidence, list) and 0 < len(evidence) <= 100
        and all(isinstance(e, dict) and isinstance(e.get('id'), str)
            and re.fullmatch(r'e[1-9]\d*', e['id']) and isinstance(e.get('quote'), str)
            and bool(e['quote'].strip()) for e in evidence)
        and len({e['id'] for e in evidence}) == len(evidence)
        and sum(len(e['quote']) for e in evidence) <= 120_000)


def verified_small_tables(db, evidence, *, conversation_id):
    """Rebuild groups from authorized locators and the same fixed extraction.

    Answer-supplied metadata is ignored. A shape alone cannot establish single
    complete cell texts: compare every retained block and reject nested or
    multiple paragraphs, missing/duplicated cells and ambiguous identities.
    """
    if not _evidence_valid(evidence):
        return []
    from .citation_scope import verified_source_metadata
    from ..source_documents import read_document
    from ..source_runtime import conversation_scope
    metadata = verified_source_metadata(db, evidence, conversation_id)
    if not metadata:
        return []
    scope = conversation_scope(db, conversation_id, writing=False)
    grouped, documents = {}, {}
    for entry in evidence:
        meta = metadata.get(entry['id'], {})
        shape, loc = meta.get('table_shape', {}), meta.get('location', {})
        rows, columns = shape.get('row_count'), shape.get('column_count')
        if (meta.get('kind') != 'word_block' or shape.get('protocol') != 'word_table_shape_v1'
                or shape.get('complete') is not True or shape.get('shape_kind') != 'ooxml_rectangular_unmerged'
                or type(rows) is not int or type(columns) is not int
                or not 2 <= rows * columns <= MAX_CELLS
                or shape.get('revision_id') != entry.get('revision_id')
                or shape.get('raw_sha256') != meta.get('raw_sha256')
                or shape.get('raw_sha256') != loc.get('raw_sha256')
                or shape.get('part_name') != loc.get('part_name')
                or shape.get('table_path') != loc.get('table_path')):
            continue
        key = (shape['revision_id'], shape['raw_sha256'], shape['part_name'], shape['table_path'])
        grouped.setdefault(key, []).append((entry, meta))
    result = []
    for (revision, raw_hash, part, path), members in grouped.items():
        shape = members[0][1]['table_shape']
        rows, columns = shape['row_count'], shape['column_count']
        if len(members) != rows * columns:
            continue
        if revision not in documents:
            documents[revision] = read_document(db, revision, scope=scope)
        doc = documents[revision]
        blocks = [b for b in doc['blocks'] if b.get('part_name') == part and b.get('table_path') == path]
        if (doc['raw_sha256'] != raw_hash or len(blocks) != rows * columns
                or any(b.get('part_name') == part and isinstance(b.get('table_path'), str)
                    and b['table_path'].startswith(path + '/') for b in doc['blocks'])):
            continue
        by_block = {b.get('block_id'): b for b in blocks}
        if len(by_block) != len(blocks):
            continue
        cells, seen, valid = [], set(), True
        for entry, meta in members:
            loc = meta['location']
            coordinate = (loc.get('table_row'), loc.get('table_column'))
            block = by_block.get(loc.get('block_id'))
            if (any(type(n) is not int for n in coordinate) or coordinate in seen
                    or not 1 <= coordinate[0] <= rows or not 1 <= coordinate[1] <= columns
                    or loc.get('column_span') != 1 or loc.get('vertical_merge') is not None
                    or loc.get('table_depth') != 0 or loc.get('logical_grid') is not True
                    or block is None or block['text'] != entry['quote']
                    or loc.get('extraction_fingerprint') != doc['fingerprint']
                    or any(block.get(field) != loc.get(field) for field in ('part_name', 'table_path',
                        'table_row', 'table_column', 'column_span', 'vertical_merge', 'table_depth', 'logical_grid', 'xml_path'))):
                valid = False
                break
            seen.add(coordinate)
            cells.append({'id': entry['id'], 'row': coordinate[0], 'column': coordinate[1],
                'quote': entry['quote']})
        if valid and len(seen) == rows * columns:
            result.append({'revision_id': revision, 'raw_sha256': raw_hash, 'part_name': part,
                'table_path': path, 'row_count': rows, 'column_count': columns,
                'cells': sorted(cells, key=lambda c: (c['row'], c['column'])),
                'metadata_sha256': digest([meta for _, meta in members])})
    return result


def _no_change(answer, reason):
    return {'answer': answer, 'changed': False, 'reason': reason, 'edits': [],
        'annotation_only': True, 'semantic_verification': 'not_performed'}


def table_context_candidate(db, answer, evidence, *, conversation_id):
    if not isinstance(answer, str) or not answer.strip() or len(answer) > MAX_ANSWER or not _evidence_valid(evidence):
        return _no_change(answer, 'input_bounds')
    groups = verified_small_tables(db, evidence, conversation_id=conversation_id)
    if not groups:
        return _no_change(answer, 'no_complete_small_table')
    ids = [e['id'] for e in evidence]
    baseline = citation_gate(answer, ids)
    if baseline['invalid_markers']:
        return _no_change(answer, 'invalid_markers')
    expected = deepcopy(parse_claims(answer, ids))
    visible = prose_for_citations(answer, preserve_inline=True)
    lines = answer.splitlines(keepends=True)
    edits, offset = [], 0
    for index, raw in enumerate(lines):
        line = raw.rstrip('\r\n')
        start = offset
        offset += len(raw)
        # Only a complete single-line ordinary paragraph. Existing parser
        # primitives supply sentence spans; no approximate Markdown mapping.
        if (not line or line != line.strip() or HEADING_RE.match(line) or LIST_ITEM_RE.match(line)
                or line.startswith(('|', '>')) or any(c in line for c in ('`', '*', '_', '<', '>', '\\'))
                or '[' in MARKER_RE.sub('', line) or ']' in MARKER_RE.sub('', line)
                or (index and lines[index - 1].strip())
                or (index + 1 < len(lines) and lines[index + 1].strip())
                or re.search(r'\.\s+\S', line)):
            continue
        for match in SENT_RE.finditer(line):
            sentence = match.group().strip()
            if not sentence or answer.count(sentence) != 1 or visible.count(sentence) != 1:
                continue
            parsed = parse_claims(sentence, ids)
            if len(parsed) != 1 or not parsed[0]['supported']:
                continue
            claim = parsed[0]
            possible = [g for g in groups if set(claim['evidence_ids']) <= {c['id'] for c in g['cells']}]
            if len(possible) != 1:
                continue
            group = possible[0]
            added = sorted({c['id'] for c in group['cells']} - set(claim['evidence_ids']), key=lambda s: int(s[1:]))
            if not 1 <= len(added) <= MAX_ADDED:
                continue
            positions = [i for i, old in enumerate(expected) if old == claim]
            markers = list(MARKER_RE.finditer(match.group()))
            if len(positions) != 1 or not markers:
                continue
            insert_at = start + match.start() + markers[-1].end()
            inserted = ''.join(f'[^{eid}]' for eid in added)
            edits.append({'start': start + match.start(), 'end': start + match.end(),
                'insert_at': insert_at, 'inserted_markers': inserted,
                'context_sha256': digest(group)})
            expected[positions[0]]['evidence_ids'] += added
    if not edits:
        return _no_change(answer, 'no_exact_prose_annotation')
    candidate = answer
    for edit in sorted(edits, key=lambda e: e['insert_at'], reverse=True):
        pos = edit['insert_at']
        candidate = candidate[:pos] + edit['inserted_markers'] + candidate[pos:]
    after = citation_gate(candidate, ids)
    if (MARKER_RE.sub('', answer) != MARKER_RE.sub('', candidate)
            or parse_claims(candidate, ids) != expected
            or any(after[field] != baseline[field] for field in ('unsupported', 'invalid_markers',
                'total_claims', 'marked_claims', 'grounded_ratio', 'passed'))):
        return _no_change(answer, 'claim_or_inheritance_changed')
    return {'answer': candidate, 'changed': True, 'protocol': PROTOCOL, 'edits': edits,
        'original_answer_sha256': digest(answer), 'answer_sha256': digest(candidate),
        'evidence_sha256': digest(evidence), 'verified_context_sha256': digest(groups),
        'annotation_only': True, 'semantic_verification': 'not_performed'}


def _has_pending(value):
    todo, count = [value], 0
    while todo:
        count += 1
        if count > 20_000:
            return True
        item = todo.pop()
        if isinstance(item, dict):
            if (item.get('review_dispatch') == 'pending' or item.get('state') == 'pending'
                    or item.get('error_code') == 'dispatch_outcome_unknown'):
                return True
            todo.extend(item.values())
        elif isinstance(item, list):
            todo.extend(item)
    return False


def apply_table_context(state, *, db, input_key, persist, before_review):
    """One durable annotation per user revision; no paid request or verdict."""
    before_review()
    if not isinstance(input_key, str) or not re.fullmatch(r'[a-f0-9]{64}', input_key):
        return {'changed': False, 'reason': 'invalid_input_identity'}
    key = 'citation_table_context/' + input_key
    if key in state.source_attribution_reviews:
        return {'changed': False, 'reason': 'context_already_claimed'}
    if _has_pending(state.source_attribution_reviews) or _has_pending(state.citation_audit_batches):
        return {'changed': False, 'reason': 'dispatch_outcome_unknown'}
    candidate = table_context_candidate(db, state.answer, state.evidence, conversation_id=state.conversation_id)
    if not candidate['changed']:
        return {'changed': False, 'reason': candidate['reason']}
    durable = deepcopy(state.snapshot())
    state.source_attribution_reviews[key] = {k: deepcopy(v) for k, v in candidate.items() if k != 'answer'}
    state.answer, state.provider_context = candidate['answer'], {}
    try:
        persist()
    except BaseException:
        for name, value in durable.items():
            setattr(state, name, deepcopy(value))
        raise
    before_review()
    return {'changed': True, 'edit_count': len(candidate['edits']),
        'annotation_only': True, 'semantic_verification': 'not_performed'}
