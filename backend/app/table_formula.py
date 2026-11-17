"""Exact stdlib-only program used both locally and in a reproducibility bundle."""

PROGRAM = r'''"""Conservative table formula verification; not a complete Excel engine."""
import hashlib
import json
import math
import re

REFERENCE = r"(?:(?:'(?:[^']|'')+'|[\w.]+)!)?\$?[A-Za-z]{1,3}\$?[1-9][0-9]{0,6}"
TOKEN = re.compile(r"\s*(?:([0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?|\.[0-9]+(?:[eE][+-]?[0-9]+)?)|(" + REFERENCE + r")|([A-Za-z_][A-Za-z_0-9]*)|([+*/^%(),:\-]))")


class Unresolved(ValueError):
    pass


def bounds(value):
    match = re.fullmatch(r"\$?([A-Za-z]{1,3})\$?([1-9][0-9]{0,6})(?::\$?([A-Za-z]{1,3})\$?([1-9][0-9]{0,6}))?", value)
    if not match:
        raise Unresolved("invalid_range")
    def col(text):
        result = 0
        for c in text.upper(): result = result * 26 + ord(c) - 64
        return result
    left, top = col(match[1]), int(match[2])
    right, bottom = col(match[3] or match[1]), int(match[4] or match[2])
    if left > right or top > bottom or right > 512 or bottom > 100000 or (right-left+1)*(bottom-top+1) > 10000:
        raise Unresolved("range_exceeds_limit")
    return left, top, right, bottom


def address(col, row):
    result = ""
    while col:
        col, digit = divmod(col-1, 26)
        result = chr(65+digit) + result
    return result + str(row)


class Evaluator:
    def __init__(self, document):
        self.cells = {(block['sheet'], cell['cell']): cell for block in document['blocks'] if block.get('sheet')
                      for cell in block.get('cells', [])}
        self.sheets = {s['name']: s for s in document['sheets']}
        self.cache, self.active, self.operations, self.dependencies = {}, [], 0, {}

    def tick(self):
        self.operations += 1
        if self.operations > 100000: raise Unresolved("operation_budget_exceeded")

    def cell(self, sheet, cell):
        self.tick()
        key = (sheet, cell.upper().replace('$', ''))
        if self.active:
            self.dependencies.setdefault(self.active[-1], set()).add(key)
        if key in self.cache: return self.cache[key]
        if key in self.active: raise Unresolved("circular_reference")
        if len(self.active) >= 64: raise Unresolved("dependency_depth_exceeded")
        meta = self.sheets.get(sheet)
        if meta is None: raise Unresolved("unknown_sheet")
        left, top, _, _ = bounds(key[1])
        if left > meta['column_count'] or top > meta['row_count']: raise Unresolved("reference_outside_retained_sheet")
        value = self.cells.get(key)
        if value and value.get('special_formula_member'):
            raise Unresolved("unsupported_formula_representation")
        if not value or value.get('value_kind') == 'blank': return None
        if value.get('value_kind') == 'error':
            raise Unresolved("error_cell_dependency")
        if value.get('value_kind') != 'formula':
            raw = value.get('raw_value')
            if value.get('value_kind') != 'number' or type(raw) not in (int,float):
                return ('nonnumeric', value.get('value_kind', 'legacy_or_unknown'))
            return self.finite(raw)
        if (value.get('formula_representation', 'scalar') != 'scalar'
                or not isinstance(value.get('formula'), str) or not value['formula'].startswith('=')):
            raise Unresolved("unsupported_formula_representation")
        self.active.append(key)
        try:
            parsed = Parser(value['formula'][1:], self, sheet).parse()
            parsed = self.numeric(parsed)
            self.cache[key] = parsed
            return parsed
        finally:
            self.active.pop()

    def finite(self, value):
        try: number = float(value)
        except (OverflowError,ValueError,TypeError): raise Unresolved("invalid_numeric_value") from None
        if not math.isfinite(number): raise Unresolved("nonfinite_numeric_value")
        return number

    def numeric(self, value):
        if value is None: raise Unresolved("blank_dependency_not_imputed")
        if isinstance(value, (list,tuple)): raise Unresolved("text_date_boolean_or_range_not_numeric")
        return self.finite(value)


class Parser:
    def __init__(self, text, engine, sheet):
        if len(text) > 4096: raise Unresolved("formula_too_long")
        self.tokens, self.index, self.engine, self.sheet, self.depth = [], 0, engine, sheet, 0
        first_function = re.match(r"\s*([A-Za-z_][A-Za-z_0-9]*)\s*\(", text)
        if first_function and first_function[1].upper() not in ('SUM','AVERAGE','MIN','MAX','COUNT'):
            raise Unresolved("unsupported_function:" + first_function[1].upper())
        start = 0
        while start < len(text):
            if not text[start:].strip(): break
            match = TOKEN.match(text, start)
            if not match: raise Unresolved("unsupported_formula_syntax")
            self.tokens.append(next((i, value) for i,value in enumerate(match.groups(),1) if value is not None))
            start = match.end()
        if len(self.tokens)>1024: raise Unresolved("formula_token_limit")

    def peek(self, value=None):
        token = self.tokens[self.index] if self.index < len(self.tokens) else None
        return token if value is None else token is not None and token[1] == value

    def pop(self, expected=None):
        self.engine.tick()
        token = self.peek()
        if token is None or expected is not None and token[1] != expected: raise Unresolved("invalid_formula_syntax")
        self.index += 1
        return token

    def parse(self):
        value = self.add()
        if self.peek() is not None: raise Unresolved("unsupported_formula_syntax")
        return value

    def add(self):
        value = self.multiply()
        while self.peek('+') or self.peek('-'):
            op = self.pop()[1]; other = self.multiply()
            value = self.engine.numeric(value) + self.engine.numeric(other) if op == '+' else self.engine.numeric(value) - self.engine.numeric(other)
        return value

    def multiply(self):
        value = self.power()
        while self.peek('*') or self.peek('/'):
            op = self.pop()[1]; other = self.power()
            if op == '/' and self.engine.numeric(other) == 0: raise Unresolved("division_by_zero")
            value = self.engine.numeric(value) * self.engine.numeric(other) if op == '*' else self.engine.numeric(value) / self.engine.numeric(other)
        return value

    def power(self):
        # Excel unary signs precede power; consecutive powers evaluate left to right.
        value = self.unary()
        while self.peek('^'):
            self.pop(); base, exponent = self.engine.numeric(value), self.engine.numeric(self.unary())
            if base == 0 and exponent == 0: raise Unresolved("zero_to_zero_undefined")
            value = base ** exponent
            if isinstance(value, complex): raise Unresolved("complex_result_unsupported")
            value = self.engine.finite(value)
        return value

    def unary(self):
        self.depth += 1
        if self.depth > 64: raise Unresolved("formula_nesting_limit")
        try:
            if self.peek('+') or self.peek('-'):
                sign = self.pop()[1]
                value = self.engine.numeric(self.unary())
                return value if sign == '+' else -value
            value = self.primary()
            while self.peek('%'):
                self.pop(); value = self.engine.numeric(value) / 100
            return value
        finally: self.depth -= 1

    def reference(self, token):
        if '[' in token or ']' in token: raise Unresolved("external_reference_unsupported")
        if '!' in token:
            sheet, cell = token.rsplit('!',1)
            if sheet.startswith("'"): sheet = sheet[1:-1].replace("''", "'")
        else: sheet, cell = self.sheet, token
        return sheet, cell.replace('$','').upper()

    def primary(self):
        kind, token = self.pop()
        if kind == 1: return self.engine.finite(token)
        if token == '(':
            value = self.add(); self.pop(')'); return value
        if kind == 2:
            sheet, cell = self.reference(token)
            if self.peek(':'):
                self.pop(); other_kind, other = self.pop()
                if other_kind != 2: raise Unresolved("invalid_range")
                other_sheet, other_cell = self.reference(other)
                if '!' in other and other_sheet != sheet: raise Unresolved("three_dimensional_range_unsupported")
                left,top,right,bottom = bounds(cell+':'+other_cell)
                return [self.engine.cell(sheet,address(col,row)) for row in range(top,bottom+1) for col in range(left,right+1)]
            return self.engine.cell(sheet,cell)
        if kind == 3:
            name = token.upper()
            if name not in ('SUM','AVERAGE','MIN','MAX','COUNT'): raise Unresolved("unsupported_function:"+name)
            self.pop('('); values = []
            if not self.peek(')'):
                while True:
                    value = self.add()
                    values.extend(value if isinstance(value,list) else [value])
                    if not self.peek(','): break
                    self.pop(',')
            self.pop(')')
            # True blank cells/text/date/bool references remain absent, not coerced to zero.
            numbers = [value for value in values if type(value) in (int,float)]
            if name == 'COUNT': return float(len(numbers))
            if not numbers: raise Unresolved("aggregate_has_no_numeric_data")
            if name == 'SUM': return math.fsum(numbers)
            if name == 'AVERAGE': return math.fsum(v/len(numbers) for v in numbers)
            return min(numbers) if name == 'MIN' else max(numbers)
        raise Unresolved("unsupported_formula_syntax")


def calculate(document, config):
    if document.get('format') != 'excel': raise ValueError('Only retained Excel structured extractions can be recomputed')
    engine = Evaluator(document)
    sheet, requested = config['sheet'], config['cell_range']
    left,top,right,bottom = bounds(requested)
    meta = engine.sheets.get(sheet)
    if meta is None or right>meta['column_count'] or bottom>meta['row_count']: raise ValueError('Selection exceeds the retained sheet')
    absolute,relative = config['absolute_tolerance'],config['relative_tolerance']
    if type(absolute) not in (int,float) or type(relative) not in (int,float) or not 0<=absolute<=1 or not 0<=relative<=1:
        raise ValueError('Invalid numeric tolerance')
    outputs=[]
    for row in range(top,bottom+1):
        for col in range(left,right+1):
            key=(sheet,address(col,row)); cell=engine.cells.get(key,{})
            if cell.get('value_kind')!='formula': continue
            result={'sheet':sheet,'cell':key[1],'formula':cell.get('formula'),'cached_value':cell.get('cached_value'),
                    'cache_state':cell.get('cache_state','unknown'),'cache_verified':False,
                    'formula_representation':cell.get('formula_representation','scalar')}
            try:
                value=engine.numeric(engine.cell(*key))
                cached=cell.get('cached_value')
                comparison=('matches' if math.isclose(value,cached,rel_tol=relative,abs_tol=absolute) else 'mismatch') if type(cached) in (int,float) and math.isfinite(cached) else 'missing_or_nonnumeric'
                result.update(status='computed',value=value,cache_comparison=comparison)
            except (Unresolved,OverflowError,ZeroDivisionError,RecursionError,TypeError,ValueError) as exc:
                result.update(status='unresolved',value=None,reason=str(exc) if isinstance(exc,Unresolved) else type(exc).__name__)
            result['dependencies']=[{'sheet':s,'cell':c} for s,c in sorted(engine.dependencies.get(key,set()))]
            outputs.append(result)
    unresolved=sum(row['status']=='unresolved' for row in outputs)
    return {'engine':'conservative_numeric_formulas/1','status':'partial' if unresolved else 'succeeded' if outputs else 'no_formulas',
            'selection':config,'outputs':outputs,'formula_count':len(outputs),'computed_count':len(outputs)-unresolved,'unresolved_count':unresolved,
            'operations':engine.operations,'formula_cache_used_for_computation':False,'unit_conversions_applied':False,
            'method':'Finite numeric arithmetic and SUM/AVERAGE/MIN/MAX/COUNT over retained numeric cells. Numeric text, dates and booleans are not coerced; blank arithmetic dependencies remain unresolved. Error cell dependencies remain unresolved, including inside aggregates. Aggregates skip other nonnumeric cells and report no numeric data rather than invent zero.',
            'supported_syntax':['+','-','*','/','^','%','SUM','AVERAGE','MIN','MAX','COUNT','local/cross-sheet A1 references and ranges'],
            'limitations':['Not a complete Excel engine','No external workbooks, macros, named ranges, dynamic arrays or volatile functions','No automatic unit conversion','Matching an unverified cache does not verify its freshness'],
            'raw_sha256':document['raw_sha256'],'extraction_fingerprint':document.get('fingerprint')}
'''


def execute(document, config):
    namespace = {"__name__": "scholarflow_table_verify"}
    exec(compile(PROGRAM, "table_reproduce.py", "exec"), namespace)
    return namespace["calculate"](document, config)
