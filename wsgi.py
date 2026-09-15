import os
from main import app
from werkzeug.serving import run_simple

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Starting server on port {port}...")
    run_simple('0.0.0.0', port, app, use_reloader=True, use_debugger=True)
