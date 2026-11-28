"""Phase 8: section deletion protection.

A section may be DELETED only when nothing refers to it. A section with
students, class schedules, faculty class records or registration forms is
ARCHIVED instead: it disappears from new enrollment and section pickers
(is_active = 0) but every record that points to it stays intact.

Used by the registrar's Manage Sections page, the dean's section hub and the
program delete paths. Callers commit or roll back.
"""

import json
from datetime import datetime

from sqlalchemy import text

from website import db
from website.models import Section, SectionArchive, EnrollmentAuditLog

ROLES = ('registrar', 'admin', 'dean', 'superadmin')


class SectionError(Exception):
    def __init__(self, *messages, blockers=None):
        self.messages = [m for m in messages if m]
        self.blockers = blockers or []
        super().__init__('; '.join(self.messages))


def _q(sql, **params):
    return db.session.execute(text(sql), params)


def _audit(action, actor, details):
    db.session.add(EnrollmentAuditLog(action=action, performed_by=actor.name, performed_role=actor.role,
                                      performed_at=datetime.now(),
                                      details=json.dumps(details, default=str, ensure_ascii=False)))


def labels(section):
    """The labels a class schedule (teaching_loads.section) can use for this section:
    "<PROGRAM_CODE> <NAME>", also for admin-style names that already start with the code."""
    code = (section.program.program_code if section.program else '') or ''
    name = (section.name or '').strip()
    out = {('%s %s' % (code, name)).strip()}
    if code and name.lower().startswith(code.lower()):
        out.add(('%s %s' % (code, name[len(code):].strip())).strip())
    return sorted(out)


def display_name(section):
    code = section.program.program_code if section.program else ''
    return labels(section)[0] if code else (section.name or '#%d' % section.id)


def usage(section):
    """[(label, count)] of everything that refers to the section (only non-zero counts)."""
    sid = section.id
    checks = [
        ('student(s) assigned', "SELECT COUNT(*) FROM applicants WHERE section_id = :s", {'s': sid}),
        ('faculty class record(s)', "SELECT COUNT(*) FROM faculty_assignments WHERE section_id = :s", {'s': sid}),
        ('registration form(s)', "SELECT COUNT(*) FROM registration_forms WHERE section_id = :s", {'s': sid}),
        ('registration-form subject line(s)', "SELECT COUNT(*) FROM registration_form_subjects WHERE section_id = :s", {'s': sid}),
    ]
    found = []
    for label, sql, params in checks:
        n = _q(sql, **params).scalar() or 0
        if n:
            found.append((label, int(n)))
    # class schedules refer to the section by label (no foreign key)
    labs = labels(section)
    sql = "SELECT COUNT(*) FROM teaching_loads WHERE section IN :labs"
    params = {'labs': tuple(labs)}
    if section.school_year:
        sql += " AND (school_year = :sy OR school_year IS NULL)"
        params['sy'] = section.school_year
    n = db.session.execute(text(sql).bindparams(_expanding('labs')), params).scalar() or 0
    if n:
        found.append(('class schedule(s)', int(n)))
    return found


def _expanding(name):
    from sqlalchemy import bindparam
    return bindparam(name, expanding=True)


def describe(blockers):
    return ', '.join('%d %s' % (n, label) for label, n in blockers)


def _section(section_id):
    sec = db.session.get(Section, int(section_id)) if section_id not in (None, '') else None
    if sec is None:
        raise SectionError('Section not found.')
    return sec


def _check_role(actor):
    if actor.role not in ROLES:
        raise SectionError('You are not allowed to change sections.')


def archive_row(section_id):
    return SectionArchive.query.filter_by(section_id=section_id).first()


def is_archived(section_id):
    row = archive_row(section_id)
    return bool(row and row.is_archived)


def archived_ids():
    return {r.section_id for r in SectionArchive.query.filter_by(is_archived=True).all()}


def delete(actor, section_id):
    """Delete an unused section. Refuses (with the reasons) when anything refers to it."""
    _check_role(actor)
    sec = _section(section_id)
    _q("SELECT id FROM sections WHERE id = :s FOR UPDATE", s=sec.id)
    found = usage(sec)
    name = display_name(sec)
    if found:
        raise SectionError('Section "%s" cannot be deleted: it has %s. Archive it instead; that keeps the history.'
                           % (name, describe(found)), blockers=found)
    snapshot = {'id': sec.id, 'program_id': sec.program_id, 'name': sec.name, 'year_level': sec.year_level,
                'semester': sec.semester, 'school_year': sec.school_year, 'max_students': sec.max_students}
    row = archive_row(sec.id)
    if row is not None:
        db.session.delete(row)
    _q("DELETE FROM sections WHERE id = :s", s=sec.id)
    db.session.expunge(sec)
    _audit('section_delete', actor, snapshot)
    return name


def archive(actor, section_id, reason):
    _check_role(actor)
    sec = _section(section_id)
    reason = (reason or '').strip()
    if not reason:
        raise SectionError('Give a reason for archiving the section.')
    row = archive_row(sec.id)
    if row is not None and row.is_archived:
        raise SectionError('Section "%s" is already archived.' % display_name(sec))
    if row is None:
        row = SectionArchive(section_id=sec.id)
        db.session.add(row)
    row.is_archived, row.was_active = True, bool(sec.is_active)
    row.reason, row.archived_by, row.archived_role, row.archived_at = reason[:500], actor.name, actor.role, datetime.now()
    row.restored_by = row.restored_at = row.restore_reason = None
    sec.is_active = False
    db.session.flush()
    _audit('section_archive', actor, {'section_id': sec.id, 'section': display_name(sec), 'reason': reason,
                                      'in_use': describe(usage(sec)) or None})
    return sec


def restore(actor, section_id, reason=None):
    _check_role(actor)
    sec = _section(section_id)
    row = archive_row(sec.id)
    if row is None or not row.is_archived:
        raise SectionError('Section "%s" is not archived.' % display_name(sec))
    row.is_archived = False
    row.restored_by, row.restored_at, row.restore_reason = actor.name, datetime.now(), ((reason or '').strip()[:500] or None)
    sec.is_active = True if row.was_active is None else bool(row.was_active)
    db.session.flush()
    _audit('section_restore', actor, {'section_id': sec.id, 'section': display_name(sec), 'reason': row.restore_reason})
    return sec


def program_section_blockers(program_id):
    """[(section label, blockers)] for the program's sections that are in use."""
    out = []
    for sec in Section.query.filter_by(program_id=program_id).all():
        found = usage(sec)
        if found:
            out.append((display_name(sec), found))
    return out

