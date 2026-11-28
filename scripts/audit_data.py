"""Read-only data-consistency audit (Phase 1).

The uploaded full.sql has the schema only, so the audit could not see real
records. Run this against a COPY of production (or production itself: every
query is a SELECT) and attach the output to the Phase 2 review.

    python scripts/audit_data.py            # uses DB_* from .env
    python scripts/audit_data.py --details  # also prints up to 20 sample rows per check
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, '.env'))

import mysql.connector  # noqa: E402

CHECKS = [
    # (id, severity, description, SELECT returning offending rows)
    ('D01', 'high', 'Same student enrolled twice in one academic term (student_enrollment_records)',
     "SELECT student_user_id, academic_term_id, COUNT(*) n FROM student_enrollment_records "
     "GROUP BY student_user_id, academic_term_id HAVING n > 1"),
    ('D02', 'high', 'Student ID / control number used by more than one applicant',
     "SELECT v, COUNT(*) n FROM (SELECT old_student_id v FROM applicants WHERE old_student_id <> '' "
     "UNION ALL SELECT control_number FROM applicants WHERE control_number <> '' AND control_number <> old_student_id) t "
     "GROUP BY v HAVING n > 1"),
    ('D03', 'high', 'Assessment balance does not equal total - paid (stored balance drift)',
     "SELECT id, student_id, description, total_amount, amount_paid, balance FROM assessments "
     "WHERE ROUND(total_amount - amount_paid, 2) <> ROUND(balance, 2) AND NOT (balance = 0 AND amount_paid >= total_amount)"),
    ('D04', 'high', 'Assessment amount_paid differs from the sum of its payment rows',
     "SELECT a.id, a.student_id, a.amount_paid, COALESCE(SUM(p.amount_paid),0) AS paid_rows FROM assessments a "
     "LEFT JOIN payments p ON p.assessment_id = a.id GROUP BY a.id, a.student_id, a.amount_paid "
     "HAVING ROUND(a.amount_paid,2) <> ROUND(paid_rows,2)"),
    ('D05', 'medium', 'Negative balances or overpayments',
     "SELECT id, student_id, total_amount, amount_paid, balance FROM assessments WHERE balance < 0 OR amount_paid > total_amount"),
    ('D06', 'medium', 'Assessment status inconsistent with amounts',
     "SELECT id, status, total_amount, amount_paid, balance FROM assessments WHERE "
     "(status='paid' AND balance > 0) OR (status='unpaid' AND amount_paid > 0) OR (status='partial' AND (amount_paid = 0 OR balance = 0))"),
    ('D07', 'medium', 'Possible duplicate payments (same student, assessment, amount, date)',
     "SELECT student_id, assessment_id, amount_paid, payment_date, COUNT(*) n FROM payments "
     "GROUP BY student_id, assessment_id, amount_paid, payment_date HAVING n > 1"),
    ('D08', 'medium', 'Same batch fee charged twice to one student',
     "SELECT student_id, batch_id, COUNT(*) n FROM assessments WHERE batch_id IS NOT NULL GROUP BY student_id, batch_id HAVING n > 1"),
    ('D09', 'medium', 'Enrolled applicants with no enrollment record for the active term',
     "SELECT a.applicant_id, a.student_user_id FROM applicants a JOIN academic_terms t ON t.is_active = 1 "
     "LEFT JOIN student_enrollment_records r ON r.student_user_id = a.student_user_id AND r.academic_term_id = t.id "
     "WHERE a.application_status = 'Enrolled' AND r.id IS NULL"),
    ('D10', 'medium', 'Applicants whose program_choice matches no program title',
     "SELECT a.applicant_id, a.program_choice FROM applicants a LEFT JOIN programs p ON TRIM(a.program_choice) = TRIM(p.title) "
     "WHERE a.program_choice IS NOT NULL AND a.program_choice <> '' AND p.program_id IS NULL"),
    ('D11', 'medium', 'Section over capacity',
     "SELECT s.id, s.program_id, s.section_name, s.max_students, COUNT(a.applicant_id) n FROM sections s "
     "JOIN applicants a ON a.section_id = s.id GROUP BY s.id, s.program_id, s.section_name, s.max_students HAVING n > s.max_students"),
    ('D12', 'medium', 'Schedules (teaching_loads) whose section label matches no section',
     "SELECT tl.id, tl.section, tl.course_code, tl.school_year FROM teaching_loads tl WHERE NOT EXISTS ("
     "SELECT 1 FROM sections s JOIN programs p ON p.program_id = s.program_id "
     "WHERE tl.section = CONCAT(p.program_code, ' ', s.section_name))"),
    ('D13', 'medium', 'More than one active academic term',
     "SELECT COUNT(*) n FROM academic_terms WHERE is_active = 1 HAVING n <> 1"),
    ('D14', 'medium', 'Student portal grades (student_grades) with no faculty class-record grade (grades) and vice versa',
     "SELECT * FROM (SELECT 'grades without student_grades' AS kind, COUNT(*) n FROM grades g JOIN applicants a ON a.applicant_id = g.student_id "
     "LEFT JOIN student_grades sg ON sg.student_user_id = a.student_user_id AND sg.subject_id = g.subject_id WHERE sg.id IS NULL "
     "UNION ALL SELECT 'student_grades without grades', COUNT(*) FROM student_grades sg LEFT JOIN applicants a ON a.student_user_id = sg.student_user_id "
     "LEFT JOIN grades g ON g.student_id = a.applicant_id AND g.subject_id = sg.subject_id WHERE g.id IS NULL) t WHERE n > 0"),
    ('D15', 'low', 'Remarks values in student_grades (should be a fixed set)',
     "SELECT remarks, COUNT(*) n FROM student_grades GROUP BY remarks"),
    ('D16', 'low', 'application_status values in use',
     "SELECT application_status, COUNT(*) n FROM applicants GROUP BY application_status"),
    ('D17', 'low', 'Subjects that reference a missing prerequisite subject',
     "SELECT s.id, s.subject_code, s.prerequisite_subject_id FROM subjects s LEFT JOIN subjects p ON p.id = s.prerequisite_subject_id "
     "WHERE s.prerequisite_subject_id IS NOT NULL AND p.id IS NULL"),
    ('D18', 'low', 'Payments with no receipt number',
     "SELECT COUNT(*) n FROM payments WHERE receipt_number IS NULL OR receipt_number = '' HAVING n > 0"),
]


def main(details=False):
    conn = mysql.connector.connect(
        host=os.getenv('DB_HOST', 'localhost'), port=int(os.getenv('DB_PORT', '3306')),
        user=os.getenv('DB_USER', 'root'), password=os.getenv('DB_PASSWORD', ''), database=os.getenv('DB_NAME'))
    conn.autocommit = True
    cur = conn.cursor(dictionary=True)
    cur.execute("SET SESSION TRANSACTION READ ONLY")
    print(f"PGPC data audit - database {os.getenv('DB_NAME')}")
    worst = 0
    for cid, sev, desc, sql in CHECKS:
        try:
            cur.execute(sql)
            rows = cur.fetchall()
        except mysql.connector.Error as e:
            print(f"[{cid}] {sev:6} ERROR {desc}: {e.msg}")
            continue
        informational = cid in ('D15', 'D16')
        status = 'info' if informational else ('OK' if not rows else f'{len(rows)} issue(s)')
        print(f"[{cid}] {sev:6} {status:12} {desc}")
        if rows and (details or informational):
            for r in rows[:20]:
                print('        ', dict(r))
        if rows and not informational and sev == 'high':
            worst = 1
    conn.close()
    return worst


if __name__ == '__main__':
    sys.exit(main('--details' in sys.argv))
