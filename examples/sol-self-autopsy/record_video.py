"""Record the deterministic incident lab to WebM using installed Chrome."""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("server", ROOT / "server.py")
assert _spec and _spec.loader
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)
OUT = ROOT / "artifacts"


def main() -> None:
    OUT.mkdir(exist_ok=True)
    httpd = server.make_server(port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                record_video_dir=str(OUT),
                record_video_size={"width": 1920, "height": 1080},
                device_scale_factor=1,
            )
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(base, wait_until="networkidle")
            page.locator("#start").click()
            page.locator("#end.show").wait_for(timeout=45_000)
            page.wait_for_timeout(3_000)
            state = page.request.get(base + "/api/state").json()
            trace = page.request.get(base + "/api/trace").json()["events"]
            required = {"http_response", "ui_signal", "agent_claim", "contradiction", "diagnosis", "patch", "verification"}
            kinds = {row["event"] for row in trace}
            assert state["value"] == "on" and state["version"] == 8, state
            assert required <= kinds, required - kinds
            assert not errors, errors
            video = page.video
            context.close()
            browser.close()
            source = Path(video.path())
            target = OUT / "sol-self-autopsy.webm"
            if source != target:
                shutil.move(source, target)
            print(f"PASS: recorded {target} with {len(trace)} trace events; backend=on/version8")
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
