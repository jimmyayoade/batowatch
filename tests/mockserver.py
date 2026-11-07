import http.server, json, os, sys, time
APP = sys.argv[1]; D = sys.argv[2]
def load(n): return open(os.path.join(D, f"bw-{n}.json"), "rb").read()
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, ct, body):
        self.send_response(code); self.send_header("Content-Type", ct); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"): return self.send(200, "text/html", open(os.path.join(APP, "www/dashboard.html"), "rb").read())
        if p.startswith("/www/"): return self.send(200, "image/png" if p.endswith("png") else "image/svg+xml", open(os.path.join(APP, p[1:]), "rb").read())
        if p == "/api/events":
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            s = json.loads(load("state")); s["now"] = time.time()
            self.wfile.write(b"data: " + json.dumps(s).encode() + b"\n\n"); self.wfile.flush(); return
        name = {"/api/state": "state", "/api/settings": "settings", "/api/alerts": "alerts", "/api/history": "history"}.get(p)
        if name: return self.send(200, "application/json", load(name))
        self.send(404, "text/plain", b"no")
http.server.ThreadingHTTPServer(("127.0.0.1", 18686), H).serve_forever()
