"""Start factory/server.py as a real process and talk HTTP to it."""
import http.client
import importlib.util
import json
import re
import shutil
import socket
import subprocess
import sys
import time

from .fixture import FACTORY_TEMPLATES


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def load_module(factory_dir, name="server.py"):
    """Import a module from `factory_dir` (default server.py, so its ROOT is that directory)."""
    spec = importlib.util.spec_from_file_location(f"factory_{name[:-3]}_{id(factory_dir)}", factory_dir / name)
    mod = importlib.util.module_from_spec(spec)
    saved, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # never leave __pycache__ in templates/
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = saved
    return mod


def copy_server(factory_dir):
    factory_dir.mkdir(parents=True, exist_ok=True)
    for name in ("server.py", "dashboard.html"):
        shutil.copy2(FACTORY_TEMPLATES / name, factory_dir / name)


class Server:
    def __init__(self, fx, factory_dir):
        self.proc = subprocess.Popen([sys.executable, "-I", str(factory_dir / "server.py"), "--port", str(free_port())],
                                     cwd=fx.root, env=fx.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 10
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            m = re.search(r"http://127\.0\.0\.1:(\d+)/dashboard\.html#t=(\S+)", line)
            if m:
                self.port, self.token = int(m.group(1)), m.group(2)
                return
            if not line and self.proc.poll() is not None:
                break
        self.stop()
        raise AssertionError(f"server did not print its URL: {self.proc.stderr.read()}")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream:
                stream.close()

    def request(self, method, path, body=None, headers=None, host=None, token=True):
        """Return (status, parsed JSON or raw bytes). `host` overrides the Host header."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        hdrs = {"Host": host or f"127.0.0.1:{self.port}"}
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
            hdrs["Content-Length"] = str(len(data))
        if token:
            hdrs["X-Factory-Token"] = self.token
        hdrs.update(headers or {})
        for k, v in hdrs.items():
            c.putheader(k, v)
        c.endheaders(data)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    def post_action(self, action, **kw):
        return self.request("POST", "/api/action", body=action, **kw)
