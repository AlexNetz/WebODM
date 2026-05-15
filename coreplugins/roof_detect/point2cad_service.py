"""
Minimal HTTP service wrapping the point2cad CLI.
Mounted into the point2cad container, started via docker-compose command override.

Endpoints:
  POST /run    {"xyzc_path": "...", "out_path": "...", "p2cad_args": {...}}
                                                        → {"task_id": "..."}
  GET  /status/<task_id>                                → {"done": bool, "success": bool,
                                                            "error": str|null,
                                                            "stdout": str, "stderr": str}

p2cad_args (alle optional, durchgereicht als CLI-Flags an point2cad.main):
  max_parallel_surfaces, num_inr_fit_attempts, seed, surfaces_multiprocessing
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import json, subprocess, threading, uuid, os, sys

jobs = {}  # task_id → {done, success, error, stdout, stderr}

# Hartkodiert auf Service-Seite — verhindert command injection und unbekannte Flags.
P2CAD_ARG_WHITELIST = {
    'max_parallel_surfaces',
    'num_inr_fit_attempts',
    'seed',
    'surfaces_multiprocessing',
}

# Cap stdout/stderr per job to keep memory + status responses bounded.
LOG_LIMIT = 30000


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length))
        xyzc_path = body['xyzc_path']
        out_path = body['out_path']
        p2cad_args = body.get('p2cad_args') or {}
        task_id = str(uuid.uuid4())
        jobs[task_id] = {
            'done': False, 'success': False, 'error': None,
            'stdout': '', 'stderr': '',
        }
        threading.Thread(
            target=_run,
            args=(task_id, xyzc_path, out_path, p2cad_args),
            daemon=True,
        ).start()
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


def _run(task_id, xyzc_path, out_path, p2cad_args=None):
    os.makedirs(out_path, exist_ok=True)
    env = {
        **os.environ,
        'PYTHONPATH': '/work/point2cad',
        # Reduce CUDA allocator fragmentation on small GPUs (~2 GiB visible).
        # Caps split-block size at 64 MB so small new requests can be served
        # from free fragments instead of triggering OOM.
        'PYTORCH_CUDA_ALLOC_CONF': 'max_split_size_mb:64',
        # Force unbuffered Python output so the line-readers see progress
        # immediately. Without this, point2cad's `print(...)` is block-buffered
        # in a non-TTY pipe and the live-log stays empty until process exit.
        'PYTHONUNBUFFERED': '1',
    }
    # Force CPU mode when GPU VRAM is too small (e.g. GTX 1050 with 2 GiB).
    # Set P2CAD_FORCE_CPU=1 in docker-compose to enable; remove for GPU mode.
    if os.environ.get('P2CAD_FORCE_CPU', '').lower() in ('1', 'true', 'yes'):
        env['CUDA_VISIBLE_DEVICES'] = ''

    cmd = [
        sys.executable, '-u', '-m', 'point2cad.main',
        '--path_in', xyzc_path,
        '--path_out', out_path,
    ]
    # Append whitelisted point2cad CLI args from the request body.
    # No fallback default — when nothing is passed, point2cad's own defaults apply.
    for k, v in (p2cad_args or {}).items():
        if k in P2CAD_ARG_WHITELIST and v is not None:
            cmd.extend(['--{}'.format(k), str(v)])

    # Popen + reader threads so /status/<task_id> returns live stdout/stderr
    # while point2cad is still running (subprocess.run with capture_output
    # buffers everything until exit — invisible to the client).
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=out_path,
        env=env,
        bufsize=1,        # line-buffered text mode
        text=True,
    )

    def _reader(stream, key):
        # Append each line to jobs[task_id][key], trimmed to LOG_LIMIT chars.
        # GIL makes single-string-append atomic enough for this use case.
        for line in iter(stream.readline, ''):
            buf = jobs[task_id][key] + line
            if len(buf) > LOG_LIMIT:
                buf = buf[-LOG_LIMIT:]
            jobs[task_id][key] = buf
        stream.close()

    t_out = threading.Thread(target=_reader, args=(proc.stdout, 'stdout'), daemon=True)
    t_err = threading.Thread(target=_reader, args=(proc.stderr, 'stderr'), daemon=True)
    t_out.start()
    t_err.start()

    rc = proc.wait()
    t_out.join(timeout=2)
    t_err.join(timeout=2)

    success = rc == 0
    stdout = jobs[task_id]['stdout']
    stderr = jobs[task_id]['stderr']
    jobs[task_id] = {
        'done': True,
        'success': success,
        'error': stderr if not success else None,
        'stdout': stdout,
        'stderr': stderr,
    }
    if not success:
        print('[point2cad] task {} FAILED:\n{}'.format(task_id, stderr), flush=True)
    else:
        print('[point2cad] task {} done'.format(task_id), flush=True)


if __name__ == '__main__':
    port = int(os.environ.get('P2CAD_PORT', 8765))
    print(f'[point2cad-service] listening on :{port}', flush=True)
    HTTPServer(('0.0.0.0', port), Handler).serve_forever()
