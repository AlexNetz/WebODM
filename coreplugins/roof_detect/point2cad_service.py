"""
Minimal HTTP service wrapping the point2cad CLI.
Mounted into the point2cad container, started via docker-compose command override.

Endpoints:
  POST /run    {"xyzc_path": "...", "out_path": "..."}  → {"task_id": "..."}
  GET  /status/<task_id>                                → {"done": bool, "success": bool, "error": str|null}
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import json, subprocess, threading, uuid, os, sys

jobs = {}  # task_id → {done, success, error}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length))
        xyzc_path = body['xyzc_path']
        out_path = body['out_path']
        task_id = str(uuid.uuid4())
        jobs[task_id] = {'done': False, 'success': False, 'error': None}
        threading.Thread(target=_run, args=(task_id, xyzc_path, out_path), daemon=True).start()
        self._respond(201, {'task_id': task_id})

    def do_GET(self):
        parts = self.path.strip('/').split('/')
        if len(parts) == 2 and parts[0] == 'status':
            task_id = parts[1]
            self._respond(200, jobs.get(task_id, {'done': False, 'success': False, 'error': 'unknown task'}))
        elif self.path == '/health':
            self._respond(200, {'ok': True})
        else:
            self._respond(404, {'error': 'not found'})

    def _respond(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # suppress per-request logging


def _run(task_id, xyzc_path, out_path):
    os.makedirs(out_path, exist_ok=True)
    env = {**os.environ, 'PYTHONPATH': '/work/point2cad'}
    result = subprocess.run(
        [
            sys.executable, '-m', 'point2cad.main',
            '--path_in', xyzc_path,
            '--path_out', out_path,
            '--max_parallel_surfaces', '1',
        ],
        capture_output=True,
        cwd=out_path,   # task-specific dir → each task gets its own tmp.obj
        env=env,
    )
    success = result.returncode == 0
    error = result.stderr.decode(errors='replace') if not success else None
    jobs[task_id] = {'done': True, 'success': success, 'error': error}
    if not success:
        print(f'[point2cad] task {task_id} FAILED:\n{error}', flush=True)
    else:
        print(f'[point2cad] task {task_id} done', flush=True)


if __name__ == '__main__':
    port = int(os.environ.get('P2CAD_PORT', 8765))
    print(f'[point2cad-service] listening on :{port}', flush=True)
    HTTPServer(('0.0.0.0', port), Handler).serve_forever()
