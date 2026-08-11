from app import create_app, db
from app.models import Student, Grade
import sys

def clear_student_records():
    """
    This script deletes all student records and their associated grades from the database.
    It will ask for confirmation before proceeding.
    """
    print("--- STUDENT RECORD DELETION SCRIPT ---")
    
    # Get a count of current students
    student_count = Student.query.count()
    if student_count == 0:
        print("No student records found in the database. Nothing to do.")
        return

    print(f"WARNING: You are about to permanently delete {student_count} student record(s)")
    print("and all of their associated grade data.")
    
    # Ask for user confirmation
    confirm = input("Are you sure you want to proceed? (yes/no): ").lower()

    if confirm == 'yes':
        try:
            print("Deleting all grade records...")
            # It's important to delete grades first because they reference students
            deleted_grades = db.session.query(Grade).delete()
            
            print("Deleting all student records...")
            deleted_students = db.session.query(Student).delete()

            db.session.commit()
            
            print("-" * 20)
            print("DELETION COMPLETE.")
            print(f"Successfully deleted {deleted_grades} grade record(s).")
            print(f"Successfully deleted {deleted_students} student record(s).")

        except Exception as e:
            db.session.rollback()
            print(f"An error occurred: {e}")
            print("Database has been rolled back. No changes were made.")
    else:
        print("Operation cancelled. No changes were made to the database.")

if __name__ == '__main__':
    # We need the app context to interact with the database
    app = create_app()
    with app.app_context():
        clear_student_records()