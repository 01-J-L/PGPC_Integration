from app import create_app, db
from app.models import Subject, FacultyAssignment, Grade
import re

app = create_app()

with app.app_context():
    print("--- Scanning for Old Format Subjects ---")
    
    # Get all subjects
    all_subjects = Subject.query.all()
    count = 0
    
    for sub in all_subjects:
        # Check if description contains lowercase letters (indicates old Title Case format)
        # We ignore 'NSTP' or subject codes, we look at the description text.
        if any(char.islower() for char in sub.description):
            print(f"Deleting OLD Subject: [{sub.code}] {sub.description}")
            
            # Clean up relationships first to prevent errors
            FacultyAssignment.query.filter_by(subject_id=sub.id).delete()
            Grade.query.filter_by(subject_id=sub.id).delete()
            
            # Delete the subject
            db.session.delete(sub)
            count += 1

    db.session.commit()
    print("------------------------------------------------")
    print(f"Cleanup Complete. Deleted {count} old subjects.")
    print("Your dropdowns should now show only the UPPERCASE standardized subjects.")