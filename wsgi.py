"""WSGI 入口，供 waitress / gunicorn 使用。

    waitress-serve --listen=127.0.0.1:8000 wsgi:app

ProxyFix 让应用可以部署在反向代理的子路径下（例如 https://example.com/schedul/）：
反向代理设置 ``X-Forwarded-Prefix: /schedul``，Flask 生成的链接会自动带上前缀。
"""

from werkzeug.middleware.proxy_fix import ProxyFix

from app import create_app

app = create_app()
app.wsgi_app = ProxyFix(
    app.wsgi_app,
    x_for=1,
    x_proto=1,
    x_host=1,
    x_port=1,
    x_prefix=1,
)
