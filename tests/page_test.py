#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# SPDX-License-Identifier: MIT
"""The alignment page's burn prompt, in headless Chrome, with this test playing the panel.

What must hold: while the calibration mark burns, the page asks for the press on
the machine's button only when the job waiting for it is the tool's own - the job
record running in the package's name, the controller waiting for the press, and
the Grbl sender kept out by the package, so that only its service can have sent
the job. Another party's job that waits for the press is named, and the press is
not asked for; nor is it for a job in the package's name while the package does
not keep the sender out (anyone else may have sent it), nor while the record is
the tool's last job's, ended (the job that waits is not that one).

The page runs in a frame of a harness page that answers the page's bridge calls
the way the control panel does, one frame per case, and the harness writes what
each frame shows into its own document, which Chrome dumps.

Needs Chrome (CHROME, or google-chrome, chromium or chrome on the PATH); without
it the test exits 77, and under GitHub Actions it fails.

Run: python3 tests/page_test.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "..", "ui", "index.html")
ID = "org.openglow.alignment"
ASK = "press it to burn the mark"

# name: (the job record, the Grbl sender's keeper); the controller waits for the press in each
CASES = {
    "own": ({"state": "running", "owner": "job:" + ID}, ID),
    "other": ({"state": "running", "owner": "job:panel-sheet"}, ID),
    "spoof": ({"state": "running", "owner": "job:" + ID}, None),
    "last": ({"state": "done", "owner": "job:" + ID}, ID),
}

HARNESS = r"""<!doctype html>
<meta charset="utf-8">
<pre id="out"></pre>
<script>
var PAGE = %(page)s, CASES = %(cases)s, ID = %(id)s, frames = {};
function answer(name, call) {
  var c = CASES[name];
  switch (call.call) {
    case 'self': return { id: ID, version: '0', tier: 'official', capabilities: [], units: 'metric' };
    case 'settings.get': return { settings: {} };
    case 'service.call': return { status: 200, body: { state: 'burning' } };
    case 'machine.status':
      return { grbl: { report: { laser: { arming: true } } },
               sender_out: { holder: c[1] ? { id: c[1], for_s: 3 } : null, notice: null } };
    case 'motion.job.state': return c[0];
    default: return {};
  }
}
window.addEventListener('message', function (ev) {
  var m = ev.data;
  if (!m || m.forgefirm !== 1 || !m.id) return;
  for (var name in frames)
    if (frames[name].contentWindow === ev.source)
      ev.source.postMessage({ forgefirm: 1, id: m.id, ok: true, value: answer(name, m) }, '*');
});
Object.keys(CASES).forEach(function (name) {
  var f = document.createElement('iframe');
  frames[name] = f;
  f.srcdoc = PAGE;
  document.body.appendChild(f);
});
setTimeout(function () {
  var shown = {};
  for (var name in frames) {
    var d = frames[name].contentDocument, b = d && d.getElementById('busytext');
    shown[name] = b ? b.textContent : null;
  }
  document.getElementById('out').textContent = 'RESULT ' + JSON.stringify(shown);
}, 3000);
</script>
"""

failures = 0


def check(ok, what):
    global failures
    print("  %s %s" % ("ok  " if ok else "FAIL", what))
    if not ok:
        failures += 1


def chrome():
    c = os.environ.get("CHROME")
    if c:
        return c
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    return None


def main():
    exe = chrome()
    if not exe:
        print("page_test: no Chrome (set CHROME)")
        return 1 if os.environ.get("GITHUB_ACTIONS") == "true" else 77
    page = open(PAGE, encoding="utf-8").read()
    js = lambda v: json.dumps(v).replace("</", "<\\/")  # noqa: E731
    with tempfile.TemporaryDirectory() as d:
        h = os.path.join(d, "harness.html")
        with open(h, "w", encoding="utf-8") as f:
            f.write(HARNESS % {"page": js(page), "cases": js(CASES), "id": js(ID)})
        out = subprocess.run(
            [exe, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
             "--user-data-dir=" + os.path.join(d, "profile"), "--virtual-time-budget=10000",
             "--dump-dom", "file://" + h],
            capture_output=True, text=True, timeout=120).stdout
    at = out.find("RESULT ")
    if at < 0:
        print("page_test: the harness wrote no result")
        print(out[-2000:])
        return 1
    shown = json.loads(out[at + 7:out.index("</pre>", at)].replace("&quot;", '"').replace("&amp;", "&")
                       .replace("&lt;", "<").replace("&gt;", ">"))
    print("the burn's prompt while a job waits for the press")
    check(ASK in (shown["own"] or ""), "the tool's own job (its name, its sender keep): the press is asked for")
    check(ASK not in (shown["other"] or "") and "job:panel-sheet" in (shown["other"] or ""),
          "another party's job: the press is not asked for, and the waiting job is named")
    check(ASK not in (shown["spoof"] or ""),
          "a job in the package's name with the sender not kept out by it: the press is not asked for")
    check(ASK not in (shown["last"] or ""),
          "the record the tool's last job's, ended: the job that waits is not that one, and the press is not asked for")
    if failures:
        print("shown:", json.dumps(shown, indent=1))
        print("FAIL: page_test, %d checks" % failures)
        return 1
    print("PASS: page_test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
