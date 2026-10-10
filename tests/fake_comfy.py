"""
Tiny in-process stand-in for the ComfyUI HTTP API, as far as comfy_client
uses it. It checks submitted workflows for dangling links and "renders" a
few bytes. It is our reading of the API, not the real server.
"""

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IMAGE_BYTES = b"\x89PNG fake image"


class FakeComfy(object):
    def __init__(self, node_classes=()):
        self.node_classes = set(node_classes)   # custom nodes "installed"
        self.object_info = {}                   # class -> spec, for combos
        self.prompts = {}                       # prompt_id -> workflow
        self.uploads = []                       # raw multipart bodies
        self.interrupted = False
        self.hold = False                       # True: jobs never finish
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.host = "127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def _handler(fake):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj, code=200, raw=False):
                body = obj if raw else json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/system_stats":
                    return self._send({"system": {}})
                if path.startswith("/object_info/"):
                    cls = path.rsplit("/", 1)[1]
                    if cls in fake.object_info:
                        return self._send({cls: fake.object_info[cls]})
                    return self._send({cls: {}} if cls in fake.node_classes else {})
                if path.startswith("/history/"):
                    pid = path.rsplit("/", 1)[1]
                    if fake.hold or pid not in fake.prompts:
                        return self._send({})
                    return self._send({pid: fake.history(fake.prompts[pid])})
                if path == "/view":
                    return self._send(IMAGE_BYTES, raw=True)
                self._send({}, 404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/prompt":
                    wf = json.loads(body)["prompt"]
                    errors = fake.validate(wf)
                    if errors:
                        return self._send({"error": errors}, 400)
                    pid = uuid.uuid4().hex
                    fake.prompts[pid] = wf
                    return self._send({"prompt_id": pid})
                if self.path == "/upload/image":
                    fake.uploads.append(body)
                    name = body.split(b'filename="')[1].split(b'"')[0].decode()
                    return self._send({"name": name, "subfolder": "nuke",
                                       "type": "input"})
                if self.path == "/interrupt":
                    fake.interrupted = True
                    return self._send({})
                self._send({}, 404)
        return Handler

    def validate(self, wf):
        errors = []
        for nid, node in wf.items():
            for key, v in node["inputs"].items():
                if isinstance(v, list) and len(v) == 2 and v[0] not in wf:
                    errors.append("%s.%s -> missing node %s" % (nid, key, v[0]))
            if node["class_type"] == "NukeLoadImagePath" \
                    and "NukeLoadImagePath" not in self.node_classes:
                errors.append("%s: unknown class NukeLoadImagePath" % nid)
        return errors

    def history(self, wf):
        outputs = {}
        for nid, node in wf.items():
            if node["_meta"]["title"].startswith("nuke_save"):
                outputs[nid] = {"images": [{"filename": "out_00001_.png",
                                            "subfolder": "nuke", "type": "output"}]}
            if node["class_type"] == "PreviewAny":
                outputs[nid] = {"text": ["the prompt that was used"]}
        return {"status": {"status_str": "success", "completed": True},
                "outputs": outputs}
