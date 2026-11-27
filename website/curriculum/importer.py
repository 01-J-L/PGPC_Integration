"""Read PGPC curriculum reference files and stage them for registrar review.

Supported inputs (the formats of the uploaded reference documents):
  * Plan of study (.docx): one table per term, in order 1st Year 1st Sem,
    1st Year 2nd Sem, ... 4th Year 2nd Sem (a table whose header says SUMMER is
    a summer term). Columns: COURSE CODE | COURSE DESCRIPTION | UNITS |
    PRE-REQUISITE | ...
  * Registration form (.xlsx): first sheet, "Course:" and "Level:" cells
    (e.g. "First Year-2nd sem"), then rows Code | Description | Units. Only the
    first copy on the sheet is read (the sheet repeats it for the registrar).

Nothing is written to curriculum tables here. `analyse()` returns rows with
issues, which are stored in a CurriculumImportBatch; `commit()` writes them
only after the registrar reviewed (and possibly edited) the rows.
"""

import hashlib
import io
import json
import re
import warnings
from decimal import Decimal, InvalidOperation

from website import db
from website.models import CurriculumImportBatch, CurriculumPrerequisite, CurriculumSubject, CurriculumVersion
from website.curriculum import service
from website.curriculum.service import CurriculumError, normalize_code, clean_code, term_label

TERM_SEQUENCE = [(1, 1), (1, 2), (2, 1), (2, 2), (3, 1), (3, 2), (4, 1), (4, 2), (5, 1), (5, 2)]
YEAR_WORDS = {'first': 1, '1st': 1, 'second': 2, '2nd': 2, 'third': 3, '3rd': 3, 'fourth': 4, '4th': 4, 'fifth': 5, '5th': 5}
CODE_LIKE = re.compile(r'^[A-Z]{2,}[A-Z\-\.]*\s?\d{1,4}[A-Z]?$')


class ImportFormatError(Exception):
    pass


def _clean(s):
    return re.sub(r'\s+', ' ', str(s or '').replace(' ', ' ')).strip()


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def parse_plan_of_study(data):
    import docx  # python-docx
    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as e:
        raise ImportFormatError(f'Could not read the Word file: {e}')
    title = next((_clean(p.text) for p in document.paragraphs if _clean(p.text)), '')
    rows, term_index = [], 0
    for table in document.tables:
        header = ' '.join(c.text for c in table.rows[0].cells).upper()
        if 'UNITS' not in header:
            continue
        if 'SUMMER' in header:
            year = TERM_SEQUENCE[max(term_index - 1, 0)][0]
            year_sem = (year, 3)
        else:
            if term_index >= len(TERM_SEQUENCE):
                raise ImportFormatError('More term tables than expected (max 5 years).')
            year_sem = TERM_SEQUENCE[term_index]
            term_index += 1
        for r in table.rows[1:]:
            cells, prev = [], None
            for c in r.cells:
                if c._tc is prev:      # merged cell repeated by python-docx
                    continue
                prev = c._tc
                cells.append(c.text)
            if len(cells) < 3:
                continue
            code = _clean(cells[0])
            if not code or code.upper() in ('COURSE CODE', 'CODE') or code.upper().startswith('TOTAL'):
                continue
            rows.append({
                'year_level': year_sem[0], 'semester': year_sem[1],
                'subject_code': code, 'description': _clean(cells[1]),
                'units': _clean(cells[2]),
                # keep line breaks: lists of prerequisites are often one per line
                'prerequisite': '\n'.join(_clean(x) for x in (cells[3] if len(cells) > 3 else '').splitlines() if _clean(x)),
            })
    if not rows:
        raise ImportFormatError('No subject tables were found. Is this a PGPC plan of study?')
    return {'kind': 'plan_of_study', 'title': title, 'rows': rows}


def _level_to_term(level):
    l = (level or '').lower()
    year = next((v for k, v in YEAR_WORDS.items() if re.search(r'\b' + k + r'\b', l) or l.startswith(k)), None)
    sem = 3 if 'summer' in l else (2 if re.search(r'2nd|second', l.split('year')[-1]) else
                                   (1 if re.search(r'1st|first', l.split('year')[-1]) else None))
    return year, sem


def parse_registration_form(data):
    import openpyxl
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        try:
            wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        except Exception as e:
            raise ImportFormatError(f'Could not read the Excel file: {e}')
    ws = wb.worksheets[0]
    course = level = None
    rows, seen = [], set()
    for raw in ws.iter_rows(values_only=True):
        vals = [v for v in raw if v not in (None, '')]
        texts = [_clean(v) for v in vals]
        if 'Course:' in texts and texts.index('Course:') + 1 < len(texts):
            course = course or texts[texts.index('Course:') + 1]
        if 'Level:' in texts and texts.index('Level:') + 1 < len(texts):
            level = level or texts[texts.index('Level:') + 1]
        if len(vals) >= 3 and isinstance(vals[2], (int, float)) and CODE_LIKE.match(texts[0].upper()):
            key = normalize_code(texts[0])
            if key in seen:      # the registrar's copy repeats the list
                break
            seen.add(key)
            rows.append({'subject_code': texts[0], 'description': texts[1], 'units': str(vals[2]), 'prerequisite': ''})
    wb.close()
    if not rows:
        raise ImportFormatError('No subject rows were found. Is this a PGPC registration form?')
    year, sem = _level_to_term(level)
    if not year or not sem:
        raise ImportFormatError(f"Could not read the year level / semester from Level: '{level}'.")
    for r in rows:
        r['year_level'], r['semester'] = year, sem
    return {'kind': 'registration_form', 'title': course or '', 'level': level, 'rows': rows}


def parse_file(filename, data):
    name = (filename or '').lower()
    if name.endswith('.docx'):
        return parse_plan_of_study(data)
    if name.endswith('.xlsx'):
        return parse_registration_form(data)
    raise ImportFormatError('Upload a plan of study (.docx) or a registration form (.xlsx).')


# ---------------------------------------------------------------------------
# prerequisite text -> structured requirements
# ---------------------------------------------------------------------------

_STANDING = re.compile(r'(?:regular\s+)?(\d)(?:st|nd|rd|th)\s*year(?:\s+standing)?|(?:regular\s+)(\d)(?:st|nd|rd|th)\s*year', re.I)
_UNITS = re.compile(r'(?:completed|complete|at least)?\s*(\d{2,3})\s*(?:academic\s+)?units', re.I)


_CODE_CHUNK = re.compile(r'[A-Z]{2,}[A-Z\-\.]*\s?\d{1,4}[A-Z]?')


def _split_parts(raw):
    """Split a prerequisite cell into single requirements. Handles lists made
    with commas, slashes, 'and' or line breaks, and several codes typed on one
    line ('STAT 101 PA 310')."""
    parts = []
    for part in re.split(r'[,;/\n]|\band\b', raw):
        t = _clean(part)
        if not t:
            continue
        chunks = _CODE_CHUNK.findall(t.upper())
        if len(chunks) > 1 and _clean(' '.join(chunks)) == _clean(t.upper()):
            parts.extend(_clean(c) for c in chunks)
        else:
            parts.append(t)
    return parts


def parse_prerequisites(raw, known_keys, own_key=None):
    """'CS 121, CC 102' -> [{'kind':'subject','code_key':'CS121','text':'CS 121'}, ...]"""
    out = []
    if not raw:
        return out
    for t in _split_parts(raw):
        key = normalize_code(t)
        if own_key and key == own_key:
            out.append({'kind': 'self', 'code_key': key, 'text': t})
            continue
        if key in known_keys:
            out.append({'kind': 'subject', 'code_key': key, 'text': t})
            continue
        m = _STANDING.search(t)
        if m and ('standing' in t.lower() or 'regular' in t.lower()):
            out.append({'kind': 'standing', 'min_year_level': int(m.group(1) or m.group(2)), 'text': t})
            continue
        m = _UNITS.search(t)
        if m:
            out.append({'kind': 'units', 'min_units': m.group(1), 'text': t})
            continue
        if CODE_LIKE.match(t.upper()):
            out.append({'kind': 'missing', 'code_key': key, 'text': t})
        else:
            out.append({'kind': 'other', 'text': t})
    return out


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------

def _units_or_none(u):
    try:
        return service.parse_units(u)
    except CurriculumError:
        return None


def analyse(parsed, target_version=None, edits=None):
    """Build review rows. `edits` maps row index -> dict of registrar edits
    (include, subject_code, description, units, year_level, semester, resolution)."""
    edits = edits or {}
    max_year = target_version.max_year_level if target_version else 4
    existing = {}
    if target_version is not None:
        for s in target_version.subjects.all():
            existing[s.code_key] = s
    rows = []
    for i, src in enumerate(parsed['rows']):
        e = edits.get(i, {})
        row = {
            'index': i,
            'subject_code': clean_code(e.get('subject_code', src['subject_code'])),
            'description': _clean(e.get('description', src['description'])),
            'units_raw': _clean(e.get('units', src['units'])),
            'year_level': int(e.get('year_level', src['year_level'])),
            'semester': int(e.get('semester', src['semester'])),
            'prerequisite_raw': src.get('prerequisite', ''),
            'include': e.get('include', True),
            'resolution': e.get('resolution'),
            'source': {k: src[k] for k in ('subject_code', 'description', 'units')},
        }
        row['code_key'] = normalize_code(row['subject_code'])
        row['units'] = _units_or_none(row['units_raw'])
        rows.append(row)

    keys_in_file = {}
    for r in rows:
        keys_in_file.setdefault(r['code_key'], []).append(r['index'])
    # A prerequisite can only point to a subject that will exist after the import:
    # an included row of this file or a subject already in the target curriculum.
    included_keys = {r['code_key'] for r in rows if r['include']}
    known = included_keys | set(existing)

    for r in rows:
        issues = []
        if not r['code_key']:
            issues.append(('error', 'Subject code is empty.'))
        if not r['description']:
            issues.append(('error', 'Description is empty.'))
        if r['units'] is None:
            issues.append(('error', f"Units '{r['units_raw'] or '(blank)'}' are missing or not a number. Enter the units or exclude the row."))
        if not 1 <= r['year_level'] <= max_year or r['semester'] not in (1, 2, 3):
            issues.append(('error', f"Invalid term (year {r['year_level']}, semester {r['semester']})."))
        dup = keys_in_file.get(r['code_key'], [])
        if len(dup) > 1 and r['index'] != dup[0]:
            issues.append(('error', f"Duplicate of row {dup[0] + 1} in this file."))
            if 'include' not in edits.get(r['index'], {}):
                r['include'] = False
        r['status'] = 'new'
        ex = existing.get(r['code_key'])
        if ex is not None:
            diffs = []
            if _clean(ex.description).lower() != r['description'].lower():
                diffs.append(f"description '{ex.description}' vs '{r['description']}'")
            if r['units'] is not None and Decimal(ex.units) != r['units']:
                diffs.append(f"units {ex.units} vs {r['units']}")
            if (ex.year_level, ex.semester) != (r['year_level'], r['semester']):
                diffs.append(f"term {term_label(ex.year_level, ex.semester)} vs {term_label(r['year_level'], r['semester'])}")
            if ex.subject_code != r['subject_code']:
                diffs.append(f"code written '{ex.subject_code}' vs '{r['subject_code']}'")
            if diffs:
                r['status'] = 'conflict'
                r['diffs'] = diffs
                if r['resolution'] not in ('keep', 'replace'):
                    issues.append(('error', 'Already in the curriculum with different values: ' + '; '.join(diffs)
                                   + '. Choose "keep existing" or "use file".'))
            else:
                r['status'] = 'same'
        r['prerequisites'] = parse_prerequisites(r['prerequisite_raw'], known, r['code_key'])
        for p in r['prerequisites']:
            if p['kind'] == 'missing':
                issues.append(('warning', f"Prerequisite '{p['text']}' is not a subject of this curriculum. It will be kept as a note "
                                          f"for the registrar, not as a checked prerequisite."))
            elif p['kind'] == 'self':
                issues.append(('warning', f"The reference lists {r['subject_code']} as its own prerequisite (a typing error in the "
                                          f"document). It will be kept as a note; correct it after import."))
            elif p['kind'] == 'other':
                issues.append(('warning', f"Prerequisite '{p['text']}' cannot be checked automatically; kept as a note."))
            elif p['kind'] == 'subject':
                pre_rows = [rows[j] for j in keys_in_file.get(p['code_key'], []) if rows[j]['include']]
                pre_term = (pre_rows[0]['year_level'], pre_rows[0]['semester']) if pre_rows else (
                    (existing[p['code_key']].year_level, existing[p['code_key']].semester) if p['code_key'] in existing else None)
                if pre_term and service.term_order(*pre_term) >= service.term_order(r['year_level'], r['semester']):
                    issues.append(('error', f"Prerequisite {p['text']} is not scheduled before {r['subject_code']}."))
        if r['status'] == 'same' and not r['prerequisites']:
            r.setdefault('note', 'Already in the curriculum; nothing to change.')
        r['issues'] = [{'level': lvl, 'message': msg} for lvl, msg in issues]
        r['blocking'] = r['include'] and any(lvl == 'error' for lvl, _ in issues)

    missing_from_file = []
    if target_version is not None:
        terms_in_file = {(r['year_level'], r['semester']) for r in rows}
        for key, s in existing.items():
            if (s.year_level, s.semester) in terms_in_file and key not in keys_in_file:
                missing_from_file.append({'subject_code': s.subject_code, 'description': s.description,
                                          'term': term_label(s.year_level, s.semester)})
    included = [r for r in rows if r['include']]
    summary = {
        'rows': len(rows), 'included': len(included),
        'new': sum(1 for r in included if r['status'] == 'new'),
        'same': sum(1 for r in included if r['status'] == 'same'),
        'conflicts': sum(1 for r in included if r['status'] == 'conflict'),
        'errors': sum(1 for r in included if r['blocking']),
        'warnings': sum(1 for r in included for i in r['issues'] if i['level'] == 'warning'),
        'missing_from_file': missing_from_file,
    }
    return rows, summary


# ---------------------------------------------------------------------------
# staging + commit
# ---------------------------------------------------------------------------

def stage_upload(program, filename, data, actor):
    if not data:
        raise ImportFormatError('The uploaded file is empty.')
    if len(data) > 10 * 1024 * 1024:
        raise ImportFormatError('The file is larger than 10 MB.')
    parsed = parse_file(filename, data)
    digest = hashlib.sha256(data).hexdigest()
    previous = (CurriculumImportBatch.query.filter_by(program_id=program.program_id, source_sha256=digest)
                .filter(CurriculumImportBatch.status != 'discarded').order_by(CurriculumImportBatch.id.desc()).first())
    _, summary = analyse(parsed)
    batch = CurriculumImportBatch(
        program_id=program.program_id, source_filename=filename[:255], source_kind=parsed['kind'],
        source_sha256=digest, status='pending_review', payload=json.dumps({'parsed': parsed, 'edits': {}}),
        summary=f"{summary['rows']} subject rows; {parsed.get('title', '')}"[:500], uploaded_by=actor.name)
    db.session.add(batch)
    db.session.flush()
    return batch, previous


def load_batch(batch):
    data = json.loads(batch.payload)
    edits = {int(k): v for k, v in data.get('edits', {}).items()}
    return data['parsed'], edits


def save_edits(batch, edits, new_fields=None):
    data = json.loads(batch.payload)
    data['edits'] = {str(k): v for k, v in edits.items()}
    if new_fields is not None:
        data['new_version'] = {k: (v or '')[:500] for k, v in new_fields.items()}
    batch.payload = json.dumps(data)


def load_new_fields(batch):
    return json.loads(batch.payload).get('new_version', {})


def edits_from_form(form, parsed):
    edits = {}
    for i, _ in enumerate(parsed['rows']):
        p = f'r{i}_'
        if f'{p}present' not in form:
            continue
        e = {'include': form.get(f'{p}include') == '1'}
        for field in ('subject_code', 'description', 'units', 'year_level', 'semester'):
            if f'{p}{field}' in form:
                e[field] = form.get(f'{p}{field}')
        if form.get(f'{p}resolution') in ('keep', 'replace'):
            e['resolution'] = form.get(f'{p}resolution')
        edits[i] = e
    return edits


def commit(batch, actor, target_version=None, new_version=None, edits=None):
    """Write the reviewed rows. Either `target_version` (an editable version)
    or `new_version` = dict(name, effective_academic_year, description) is given.
    Runs in the caller's transaction; raises CurriculumError on any problem."""
    if batch.status != 'pending_review':
        raise CurriculumError('This import was already processed.')
    parsed, saved_edits = load_batch(batch)
    edits = edits if edits is not None else saved_edits
    if new_version:
        version = service.create_version(batch.program_id, new_version.get('name'), new_version.get('effective_academic_year'),
                                         actor, description=new_version.get('description'),
                                         max_year_level=new_version.get('max_year_level', 4),
                                         source_reference=batch.source_filename)
    elif target_version is not None:
        version = target_version
        if version.program_id != batch.program_id:
            raise CurriculumError('The selected curriculum belongs to another program.')
        service._require_editable(service._lock_version(version.id))
    else:
        raise CurriculumError('Choose a curriculum to import into, or create a new one.')

    rows, summary = analyse(parsed, version, edits)
    blocking = [r for r in rows if r['blocking']]
    if blocking:
        raise CurriculumError(f"{len(blocking)} included row(s) still have errors. Fix or exclude them.",
                              *[f"Row {r['index'] + 1} ({r['subject_code'] or '?'}): "
                                + '; '.join(i['message'] for i in r['issues'] if i['level'] == 'error') for r in blocking[:10]])
    included = [r for r in rows if r['include']]
    if not included:
        raise CurriculumError('No rows are selected for import.')

    by_key = {s.code_key: s for s in version.subjects.all()}
    created = replaced = kept = 0
    order = {}
    for r in included:
        key = r['code_key']
        if r['status'] == 'same':
            kept += 1
            continue
        if r['status'] == 'conflict':
            if r['resolution'] == 'keep':
                kept += 1
                continue
            s = by_key[key]
            before = {'code': s.subject_code, 'description': s.description, 'units': s.units,
                      'term': term_label(s.year_level, s.semester)}
            s.subject_code, s.description, s.units = r['subject_code'], r['description'], r['units']
            s.year_level, s.semester = r['year_level'], r['semester']
            service.audit(version.id, 'subject', s.id, 'import_replace',
                          {'batch': batch.id, 'before': before, 'after': r['subject_code']}, actor)
            replaced += 1
            continue
        t = (r['year_level'], r['semester'])
        order[t] = order.get(t, 0) + 1
        s = CurriculumSubject(curriculum_version_id=version.id, year_level=r['year_level'], semester=r['semester'],
                              subject_code=r['subject_code'], code_key=key, description=r['description'],
                              units=r['units'], sort_order=order[t])
        db.session.add(s)
        by_key[key] = s
        created += 1
    db.session.flush()

    prereqs_added = 0
    for r in included:
        if r['status'] == 'conflict' and r['resolution'] == 'keep':
            continue
        subj = by_key[r['code_key']]
        have = {(p.kind, p.prerequisite_subject_id, p.min_year_level, p.requirement_text) for p in subj.prerequisites}
        for p in r['prerequisites']:
            rec = CurriculumPrerequisite(curriculum_subject_id=subj.id, requirement_text=p['text'][:255])
            if p['kind'] == 'subject':
                rec.kind, rec.prerequisite_subject_id = 'subject', by_key[p['code_key']].id
                rec.requirement_text = None
            elif p['kind'] == 'standing':
                rec.kind, rec.min_year_level = 'standing', p['min_year_level']
            elif p['kind'] == 'units':
                rec.kind, rec.min_units = 'units', Decimal(p['min_units'])
            else:  # 'missing', 'self' or 'other' -> kept as a note, never as a checked link
                rec.kind = 'other'
                if p['kind'] == 'missing':
                    rec.requirement_text = f"{p['text']} (not in this curriculum - check the reference)"[:255]
                elif p['kind'] == 'self':
                    rec.requirement_text = f"{p['text']} (the reference lists the subject itself - check the reference)"[:255]
            sig = (rec.kind, rec.prerequisite_subject_id, rec.min_year_level, rec.requirement_text)
            if sig in have:
                continue
            have.add(sig)
            db.session.add(rec)
            prereqs_added += 1

    batch.status = 'imported'
    batch.target_version_id = version.id
    batch.decided_by, batch.decided_at = actor.name, db.func.now()
    save_edits(batch, edits)
    result = {'batch': batch.id, 'file': batch.source_filename, 'created': created, 'replaced': replaced,
              'kept': kept, 'prerequisites': prereqs_added}
    service.audit(version.id, 'version', version.id, 'import', result, actor)
    return version, result
