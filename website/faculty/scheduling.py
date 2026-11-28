"""Curriculum-aware helpers for section scheduling.

The scheduling screens used to list subjects from the legacy `courses` table.
The registrar now keeps versioned curricula (`curriculum_versions` /
`curriculum_subjects`), and one program can have several active versions at the
same time: new freshmen follow the newest plan while older batches keep theirs.

These helpers let a section pick the curriculum version it is scheduled under
and make the subject dropdowns, conflict checks and semester filters use that
version instead of `courses`.

Two small tables support this without altering the legacy ones:

* `section_curricula`  - the version chosen for a section
* `teaching_load_terms` - the term/version a scheduled class belongs to

`db.create_all()` creates both on startup.
"""

from website import db
from website.curriculum.service import normalize_code
from website.models import (
    Course,
    CurriculumSubject,
    CurriculumVersion,
    Section,
    SectionCurriculum,
    TeachingLoad,
    TeachingLoadTerm,
)

# Archived versions are still schedulable: the cohorts that entered under them
# keep their plan. Drafts are not offered.
SCHEDULABLE_STATUSES = ('active', 'archived')


def find_section(program_id, section_name):
    """Return the `sections` row for a grid URL.

    Section names come in two shapes: the faculty screens use "1A" while the
    admin "Manage Sections" page stores "BSCS 1A". Match both.
    """
    wanted = (section_name or '').strip().lower()
    if not wanted:
        return None
    for section in Section.query.filter_by(program_id=program_id).all():
        name = (section.name or '').strip().lower()
        if name == wanted or name.endswith(' ' + wanted):
            return section
    return None


def program_versions(program_id):
    """Curriculum versions that can be scheduled, default/newest first."""
    return (
        CurriculumVersion.query
        .filter(
            CurriculumVersion.program_id == program_id,
            CurriculumVersion.status.in_(SCHEDULABLE_STATUSES),
        )
        .order_by(
            CurriculumVersion.is_default_for_new.desc(),
            CurriculumVersion.effective_academic_year.desc(),
            CurriculumVersion.id.desc(),
        )
        .all()
    )


def get_section_version(section, program_id, requested_id=None):
    """The curriculum a section uses.

    Order: an explicitly requested version, the saved section choice, the
    program's default-for-new-students version, then the newest schedulable
    version. Returns None when the program has no curricula at all, in which
    case callers keep the legacy course list.
    """
    versions = program_versions(program_id)
    if not versions:
        return None
    by_id = {v.id: v for v in versions}

    if requested_id:
        try:
            requested = by_id.get(int(requested_id))
        except (TypeError, ValueError):
            requested = None
        if requested is not None:
            return requested

    if section is not None:
        link = db.session.get(SectionCurriculum, section.id)
        if link is not None and link.curriculum_version_id in by_id:
            return by_id[link.curriculum_version_id]

    for version in versions:
        if version.is_default_for_new:
            return version
    return versions[0]


def save_section_version(section, version, actor_name=None):
    """Remember which curriculum a section is scheduled under."""
    if section is None or version is None:
        return
    link = db.session.get(SectionCurriculum, section.id)
    if link is None:
        link = SectionCurriculum(section_id=section.id, curriculum_version_id=version.id)
        db.session.add(link)
    elif link.curriculum_version_id != version.id:
        link.curriculum_version_id = version.id
    link.updated_by = actor_name
    db.session.commit()


def subjects_for(version, year_level, semester):
    """Curriculum subjects offered to a section in one term."""
    if version is None or not year_level:
        return []
    return (
        CurriculumSubject.query
        .filter_by(curriculum_version_id=version.id, year_level=year_level, semester=semester)
        .order_by(CurriculumSubject.sort_order, CurriculumSubject.subject_code)
        .all()
    )


def subject_choices(version, year_level, semester, extra_loads=None):
    """(value, label) pairs for the subject dropdowns.

    Values are normalized codes so "GE 101" and "GE101" are the same subject.
    Loads that are already scheduled but are not part of the chosen curriculum
    are kept in the list (marked legacy) so they can still be edited.
    """
    choices = []
    seen = set()
    for subject in subjects_for(version, year_level, semester):
        key = normalize_code(subject.code_key or subject.subject_code)
        if key in seen:
            continue
        seen.add(key)
        choices.append((key, f"{subject.subject_code} - {subject.description}"))

    for load in extra_loads or []:
        key = normalize_code(load.course_code)
        if not key or key in seen:
            continue
        seen.add(key)
        choices.append((key, f"{load.course_code} - {load.course_description or ''} (legacy)"))
    return choices


def find_course(program_id, code):
    """Legacy `courses` row for a code, ignoring spacing/case."""
    key = normalize_code(code)
    if not key:
        return None
    for course in Course.query.filter_by(program_id=program_id).all():
        if normalize_code(course.course_code) == key:
            return course
    return None


def ensure_course(program_id, subject_code, description, units, year_level, semester):
    """Make sure a curriculum subject also exists in the legacy `courses` table.

    Other screens (faculty schedule, analytics, workload) still read `courses`,
    so a subject added to a curriculum after the last import would otherwise be
    invisible to them. Existing rows are never changed.
    """
    course = find_course(program_id, subject_code)
    if course is not None:
        return course
    try:
        units_int = int(round(float(units)))
    except (TypeError, ValueError):
        units_int = 3
    course = Course(
        program_id=program_id,
        course_code=subject_code,
        course_description=description or subject_code,
        units=units_int,
        year=year_level or 1,
        semester=semester or 1,
    )
    db.session.add(course)
    return course


def _from_course(course, curriculum_subject=None):
    return {
        'code': course.course_code,
        'description': course.course_description,
        'units': course.units,
        'year_level': course.year,
        'semester': course.semester,
        'curriculum_subject': curriculum_subject,
        'course': course,
    }


def resolve_subject(version, code, program_id):
    """Find what a posted subject value refers to.

    Returns a dict with the fields needed for a teaching load. The subject is
    looked up in the chosen curriculum first, then in the legacy courses table.
    Older pages and scripts post the numeric `courses.id`, so that is accepted
    too and mapped back to its code.
    """
    raw = (code or '').strip()
    if not raw:
        return None

    # Legacy numeric course id (the dropdown used to post Course.id).
    if raw.isdigit():
        course = db.session.get(Course, int(raw))
        if course is not None:
            return _from_course(course)

    key = normalize_code(raw)
    if not key:
        return None

    if version is not None:
        subject = (
            CurriculumSubject.query
            .filter_by(curriculum_version_id=version.id, code_key=key)
            .first()
        )
        if subject is not None:
            course = ensure_course(program_id, subject.subject_code, subject.description,
                                   subject.units, subject.year_level, subject.semester)
            result = _from_course(course, subject)
            # Keep the curriculum's official spelling and units.
            result['code'] = subject.subject_code
            result['description'] = subject.description
            result['units'] = subject.units
            return result

    course = find_course(program_id, key)
    if course is not None:
        return _from_course(course)
    return None


def set_load_term(load, semester, version):
    """Record the term (and curriculum) of a teaching load."""
    term = db.session.get(TeachingLoadTerm, load.id)
    if term is None:
        term = TeachingLoadTerm(teaching_load_id=load.id, semester=semester)
        db.session.add(term)
    term.semester = semester
    if version is not None:
        term.curriculum_version_id = version.id
    return term


def load_semester(load):
    """The semester a load belongs to (term row first, then its course row)."""
    term = db.session.get(TeachingLoadTerm, load.id)
    if term is not None:
        return term.semester
    course = Course.query.filter_by(course_code=load.course_code).first()
    return course.semester if course else 1


def load_ids_for_semester(semester, program_id=None):
    """IDs of teaching loads that belong to a semester.

    New loads use `teaching_load_terms`; legacy loads (no term row) keep the
    old behaviour and inherit the term of their `courses` row. When
    `program_id` is given only that program's legacy courses are considered.
    """
    try:
        semester = int(semester)
    except (TypeError, ValueError):
        return set()

    explicit = dict(
        db.session.query(TeachingLoadTerm.teaching_load_id, TeachingLoadTerm.semester).all()
    )
    ids = {load_id for load_id, sem in explicit.items() if sem == semester}

    legacy_query = TeachingLoad.query
    if explicit:
        legacy_query = legacy_query.filter(~TeachingLoad.id.in_(list(explicit)))

    course_query = Course.query.filter_by(semester=semester)
    if program_id:
        course_query = course_query.filter_by(program_id=program_id)
    legacy_codes = {normalize_code(course.course_code) for course in course_query.all()}
    if legacy_codes:
        for load in legacy_query.all():
            if normalize_code(load.course_code) in legacy_codes:
                ids.add(load.id)
    return ids
