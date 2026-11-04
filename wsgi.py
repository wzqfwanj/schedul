"""WSGI 入口，供 waitress / gunicorn 使用：waitress-serve --listen=127.0.0.1:8000 wsgi:app"""

from app import create_app

app = create_app()
