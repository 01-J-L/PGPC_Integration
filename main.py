import os
import sys
from dotenv import load_dotenv
from werkzeug.middleware.dispatcher import DispatcherMiddleware

load_dotenv()


from website import create_app as create_admission_app
admission_app = create_admission_app()


grading_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'grading_project'))
sys.path.insert(0, grading_dir)


from app import create_app as create_grading_app
grading_app = create_grading_app()

app = DispatcherMiddleware(admission_app, {
    '/grading': grading_app 
})

if __name__ == "__main__":
    from werkzeug.serving import run_simple
    # For local testing
    run_simple('127.0.0.1', 5000, app, use_reloader=True)