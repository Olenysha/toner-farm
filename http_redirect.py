"""HTTP → HTTPS редиректор для host-сети Docker.

Всё, что пришло на HTTP-порт (обычно 80), получает 301 на
https://<тот же хост>[:порт]. Позволяет открывать сайт просто по имени
«toner-farm01» — даже если браузер пошёл по http.

Запуск (из entrypoint.sh):
    python http_redirect.py 80 443
"""
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 80
HTTPS_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 443
# 443 — дефолтный порт https, в Location его указывать не нужно
PORT_SUFFIX = '' if HTTPS_PORT == 443 else f':{HTTPS_PORT}'


class RedirectHandler(BaseHTTPRequestHandler):
    def _redirect(self):
        host = (self.headers.get('Host') or '').split(':')[0] or 'localhost'
        self.send_response(301)
        self.send_header('Location', f'https://{host}{PORT_SUFFIX}{self.path}')
        self.send_header('Content-Length', '0')
        self.end_headers()

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = _redirect

    def log_message(self, *args):
        pass  # тихий режим — редирект не спамит в лог


if __name__ == '__main__':
    print(f'[redirect] :{LISTEN_PORT} -> https://<host>{PORT_SUFFIX}', flush=True)
    ThreadingHTTPServer(('0.0.0.0', LISTEN_PORT), RedirectHandler).serve_forever()
