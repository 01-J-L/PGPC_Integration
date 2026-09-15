import os
from dotenv import load_dotenv

load_dotenv()

from website import create_app
app = create_app()

if __name__ == "__main__":
    from werkzeug.serving import run_simple
    run_simple('127.0.0.1', 5000, app, use_reloader=True)
