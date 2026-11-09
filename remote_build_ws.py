#!/usr/bin/env python3
"""Remote build workspace.

    PUT  /files/<path>   write a file
    GET  /files/<path>   read a file
    POST /build          unpack (optional) and run one command
    GET  /status         {"phase","dir","message","log"}
    GET  /shutdown       stop the server
    GET  /healthz        liveness, no token

Everything is relative to one fixed workspace root. The build runs in `dir`
and the artifact is just a file: GET /files/<path>.

Stdlib only. Cross-platform.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

MAX_UPLOAD = 512 * 1024 * 1024
LOG_TAIL_LINES = 200
CHUNK = 1 << 20


class State:
    def __init__(self, args):
        self.args = args
        self.root = os.path.realpath(args.root)
        self.lock = threading.Lock()
        self.phase = "idle"
        self.message = ""
        self.dir = args.build_dir
        self.log_path = os.path.join(self.root, "build.log")
        self.server = None

    def set(self, phase, message=""):
        with self.lock:
            self.phase = phase
            if message:
                self.message = "[%s] %s" % (time.strftime("%H:%M:%S"), message)


STATE = None


def log_tail(path):
    try:
        with open(path, "rb") as f:
            data = f.read()[-20000:]
        text = data.decode("utf-8", "replace")
        return "\n".join(text.splitlines()[-LOG_TAIL_LINES:])
    except FileNotFoundError:
        return ""


def safe_join(root, rel):
    rel = unquote(rel).lstrip("/")
    target = os.path.realpath(os.path.join(root, rel))
    if target != root and not target.startswith(root + os.sep):
        raise ValueError("path escapes the workspace: " + rel)
    return target


def safe_extract(zip_path, dest):
    dest = os.path.realpath(dest)
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            target = os.path.realpath(os.path.join(dest, member.filename))
            if target != dest and not target.startswith(dest + os.sep):
                raise ValueError("zip contains an unsafe path: " + member.filename)
        zf.extractall(dest)


def build_worker(state, command, zip_name, dir_name):
    try:
        workdir = safe_join(state.root, dir_name or state.args.build_dir)
        if zip_name:
            state.set("extracting", "extracting %s -> %s" % (zip_name, os.path.relpath(workdir, state.root)))
            zip_path = safe_join(state.root, zip_name)
            if os.path.isdir(workdir):
                shutil.rmtree(workdir)
            os.makedirs(workdir, exist_ok=True)
            safe_extract(zip_path, workdir)
        elif not os.path.isdir(workdir):
            raise RuntimeError("build dir not found: " + os.path.relpath(workdir, state.root))
        with state.lock:
            state.dir = os.path.relpath(workdir, state.root).replace(os.sep, "/")
        state.set("building", "cwd=%s cmd=%s" % (state.dir, command))
        with open(state.log_path, "wb") as log:
            log.write(("[%s] build started: %s\n" % (time.strftime("%H:%M:%S"), command)).encode("utf-8"))
            proc = subprocess.run(
                command,
                shell=True,
                cwd=workdir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=state.args.timeout,
            )
            log.write(proc.stdout or b"")
        if proc.returncode != 0:
            raise RuntimeError("build exited with code %d" % proc.returncode)
        state.set("done", "build finished")
    except subprocess.TimeoutExpired:
        state.set("error", "build timed out after %ds" % state.args.timeout)
    except Exception as e:  # noqa: BLE001
        state.set("error", str(e))


class Handler(BaseHTTPRequestHandler):
    server_version = "remote-build/1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s - %s\n" % (time.strftime("%H:%M:%S"), self.address_string(), fmt % args))

    def _auth(self):
        token = STATE.args.token
        if not token:
            return True
        q = parse_qs(urlparse(self.path).query)
        return q.get("token", [""])[0] == token or self.headers.get("X-Build-Token", "") == token

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code, text):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file_rel(self):
        path = urlparse(self.path).path
        if not path.startswith("/files/"):
            return None
        return path[len("/files/"):]

    def do_PUT(self):
        if not self._auth():
            return self._text(401, "bad token")
        rel = self._file_rel()
        if rel is None:
            return self._text(404, "not found")
        try:
            target = safe_join(STATE.root, rel)
        except ValueError as e:
            return self._text(400, str(e))
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > MAX_UPLOAD:
            return self._json(413, {"error": "too large"})
        os.makedirs(os.path.dirname(target), exist_ok=True)
        remaining = length
        with open(target, "wb") as f:
            while remaining > 0:
                chunk = self.rfile.read(min(CHUNK, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        if remaining != 0:
            return self._json(400, {"error": "short upload"})
        return self._json(201, {"written": os.path.relpath(target, STATE.root).replace(os.sep, "/")})

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._text(200, "ok")
        if not self._auth():
            return self._text(401, "bad token")
        if path == "/status":
            with STATE.lock:
                snap = {"phase": STATE.phase, "dir": STATE.dir,
                        "message": STATE.message, "log": log_tail(STATE.log_path)}
            return self._json(200, snap)
        if path == "/shutdown":
            self._json(200, {"bye": True})
            threading.Thread(target=shutdown_later, daemon=True).start()
            return None
        rel = self._file_rel()
        if rel is not None:
            try:
                target = safe_join(STATE.root, rel)
            except ValueError as e:
                return self._text(400, str(e))
            if not os.path.isfile(target):
                return self._text(404, "not found")
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(os.path.getsize(target)))
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % os.path.basename(target))
            self.end_headers()
            with open(target, "rb") as f:
                shutil.copyfileobj(f, self.wfile, CHUNK)
            return None
        return self._text(404, "not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._auth():
            return self._text(401, "bad token")
        if path == "/build":
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length > 0 else b""
            command = STATE.args.build_command
            zip_name = ""
            dir_name = ""
            if raw:
                try:
                    body = json.loads(raw) or {}
                except Exception:
                    return self._json(400, {"error": "body must be JSON"})
                command = body.get("command", command)
                zip_name = body.get("zip", "")
                dir_name = body.get("dir", "")
            try:
                if zip_name and not os.path.isfile(safe_join(STATE.root, zip_name)):
                    return self._json(404, {"error": "zip not found: " + zip_name})
                workdir = safe_join(STATE.root, dir_name or STATE.args.build_dir)
            except ValueError as e:
                return self._text(400, str(e))
            with STATE.lock:
                if STATE.phase in ("extracting", "building"):
                    return self._json(409, {"error": "build already running"})
                STATE.phase = "extracting" if zip_name else "building"
            threading.Thread(
                target=build_worker,
                args=(STATE, command, zip_name, dir_name),
                daemon=True,
            ).start()
            return self._json(202, {
                "accepted": True,
                "dir": os.path.relpath(workdir, STATE.root).replace(os.sep, "/"),
                "command": command,
                "zip": zip_name,
            })
        if path == "/shutdown":
            self._json(200, {"bye": True})
            threading.Thread(target=shutdown_later, daemon=True).start()
            return None
        return self._text(404, "not found")


def shutdown_later():
    time.sleep(0.3)
    os._exit(0)


def watcher(state):
    deadline = time.time() + state.args.max_lifetime
    while time.time() < deadline:
        time.sleep(2)
    print("[%s] max lifetime reached; exiting" % time.strftime("%H:%M:%S"), flush=True)
    os._exit(0)


def main():
    global STATE
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--token", default="")
    p.add_argument("--root", default="")
    p.add_argument("--build-dir", default="src")
    p.add_argument("--build-command", default="cargo build --release")
    p.add_argument("--timeout", type=int, default=3600)
    p.add_argument("--max-lifetime", type=int, default=7200)
    args = p.parse_args()

    if not args.root:
        args.root = tempfile.mkdtemp(prefix="remote-build-")
    os.makedirs(args.root, exist_ok=True)

    STATE = State(args)
    STATE.server = ThreadingHTTPServer((args.host, args.port), Handler)
    threading.Thread(target=watcher, args=(STATE,), daemon=True).start()

    print("[%s] workspace %s  dir %s  command %s" % (time.strftime("%H:%M:%S"), STATE.root, args.build_dir, args.build_command), flush=True)
    print("[%s] listening on %s:%d" % (time.strftime("%H:%M:%S"), args.host, args.port), flush=True)
    try:
        STATE.server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
