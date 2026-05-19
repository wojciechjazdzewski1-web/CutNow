"""Punkt wejścia dla Gunicorn na Render."""
from app import app as application
from storage import init_storage

init_storage()
