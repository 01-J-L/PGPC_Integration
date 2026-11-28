"""Regressions found while rehearsing the VPS update on a copy of the original database."""

from conftest import _uniq
from test_phase8_sections import faculty, as_faculty


def test_section_grid_for_program_without_program_name(app, client, dbx):
    # Programs added on the admission side have a title but no program_name; the scheduling
    # grid used to crash on None.upper() for them.
    code = _uniq('NP').upper()[:10]
    pid = code.lower()
    dbx.x("INSERT INTO programs (program_id, title, program_code, program_name) VALUES (%s,%s,%s,NULL)",
          (pid, 'Titled Only ' + code, code))
    dbx.x("INSERT INTO sections (program_id, year_level, section_name, section_letter, max_students, is_active, semester, school_year) "
          "VALUES (%s,'1st Year','1A','A',40,1,1,'2026-2027')", (pid,))
    as_faculty(client, faculty(dbx, 'dean'))
    resp = client.get(f'/faculty/section-grid/prog{pid}/sec1A/sem1')
    assert resp.status_code == 200
    assert ('TITLED ONLY ' + code) in resp.get_data(as_text=True)
