"""WSGI entry point.

Development:  flask run --debug
Production:   gunicorn --workers 4 --bind 0.0.0.0:8000 wsgi:app
"""

from __future__ import annotations

from app import create_app

app = create_app()

if __name__ == "__main__":
    # Convenience for `python wsgi.py`; prefer `flask run` locally and a real
    # WSGI server in production -- this one is single-threaded and unhardened.
    app.run(host="127.0.0.1", port=5000, debug=app.config.get("DEBUG", False))
