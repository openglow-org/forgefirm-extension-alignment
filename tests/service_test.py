#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# SPDX-License-Identifier: MIT
"""The alignment service's tool, over a machine that is a few lines of Python.

What must hold: the tool starts only on an idle machine and with the Grbl sender
kept out, then moves the head by the offset; Done moves it back to the spot and lets
the sender back in; the head's distance from the spot is kept exact through a move
that stops short, so Done always ends on the spot; the page's silence ends the tool,
but a freeze does not count as silence; the operator's release ends it; a service
that restarts in the middle of a use finishes it, unless the use is old; and the
calibration saves the head's move as the new offset.

Run: python3 tests/service_test.py
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bin"))
import run  # noqa: E402

failures = 0


def check(ok, what):
    global failures
    print("  %s %s" % ("ok  " if ok else "FAIL", what))
    if not ok:
        failures += 1


class ApiError(Exception):
    def __init__(self, status, words):
        super().__init__(words)
        self.status, self.words = status, words


class FakeMachine:
    def __init__(self):
        self.x, self.y = 100.0, 50.0
        self.state = "idle"
        self.released = False
        self.kept_out = False
        self.sender_refuse = None       # words the machine answers a claim with
        self.short = None               # (fraction) the next jog goes only this part of the way
        self.jog_refuse = None
        self.jogs = []                  # (x, y, feed)
        self.outs = []                  # sender_out words, in order
        self.cfg = {"offset_x_mm": 24.6, "offset_y_mm": 0.0, "jog_feed": 3000, "burn_power": 20, "burn_feed": 600}
        self.programs = []
        self.canceled = 0

    def status(self):
        return {"state": self.state, "pos": {"x": self.x, "y": self.y}, "motors_released": False}

    def jog(self, x, y, feed):
        if self.jog_refuse:
            raise ApiError(409, self.jog_refuse)
        f = 1.0
        if self.short is not None:
            f, self.short = self.short, None
        self.jogs.append((round(x, 3), round(y, 3), feed))
        self.x = round(self.x + x * f, 3)
        self.y = round(self.y + y * f, 3)

    def cancel(self):
        self.canceled += 1

    def sender_out(self, out):
        if out and self.sender_refuse:
            raise ApiError(409, self.sender_refuse)
        self.outs.append(out)
        self.kept_out = out
        if not out:
            self.released = False
        return {"out": out, "released": False}

    def sender_state(self):
        return {"out": self.kept_out, "released": self.released}

    def settings(self):
        return dict(self.cfg)

    def set_settings(self, **patch):
        self.cfg.update(patch)

    def job(self, text):
        self.programs.append(text)

    def words(self, e):
        return run.Machine.words(e)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def world():
    d = tempfile.mkdtemp(prefix="alignment-test.")
    m, c = FakeMachine(), Clock()
    wall = [1_700_000_000.0]
    tool = run.Tool(m, d, clock=c, wall=lambda: wall[0], sleep=c.sleep)
    tool.offset_now()
    return tool, m, c, d, wall


def drain(tool):
    while tool.jobs:
        tool.turn()


def call(tool, method, path, body=None):
    try:
        return tool.handle(method, path, body or {})
    except run.Refused as e:
        return {"refused": str(e)}


def main():
    print("in use and done")
    tool, m, c, d, wall = world()
    got = call(tool, "POST", "/on")
    check(got["state"] == "starting", "on: the page is told it starts: %s" % got["state"])
    drain(tool)
    check(m.outs == [True] and m.jogs == [(24.6, 0.0, run.MOVE_FEED)], "the sender kept out first, then the move: %s %s"
          % (m.outs, m.jogs))
    check(tool.state == "on" and tool.moved == (24.6, 0.0) and (m.x, m.y) == (124.6, 50.0),
          "in use, the head moved by the offset")
    check(os.path.exists(os.path.join(d, "tool.json")), "and the use is written down")
    call(tool, "POST", "/jog", {"x": 2.5, "y": -1})
    drain(tool)
    check((m.x, m.y) == (127.1, 49.0) and tool.moved == (24.6, 0.0) and m.jogs[-1][2] == 3000,
          "a jog moves the head at the jog speed, and not the distance to the spot")
    call(tool, "POST", "/off")
    drain(tool)
    check((m.x, m.y) == (102.5, 49.0) and m.jogs[-1][2] == run.MOVE_FEED, "done: the head on the spot the crosshair marked")
    check(m.outs[-1] is False and tool.state == "off" and tool.moved is None and tool.message == run.MARK_DONE,
          "then the sender let back in, and the tool off")
    check(not os.path.exists(os.path.join(d, "tool.json")), "and the record gone")
    check("refused" in call(tool, "POST", "/off") and "refused" in call(tool, "POST", "/jog", {"x": 1}),
          "done and a jog are refused with the tool off")
    shutil.rmtree(d)

    print("refusals")
    tool, m, c, d, wall = world()
    m.state = "running"
    call(tool, "POST", "/on")
    drain(tool)
    check(tool.state == "off" and "not idle" in tool.error and not m.outs and not m.jogs,
          "a busy machine: refused, nothing kept out, nothing moved: %s" % tool.error)
    m.state = "idle"
    m.sender_refuse = "the Grbl sender is using the machine: wait until it is done"
    call(tool, "POST", "/on")
    drain(tool)
    check(tool.state == "off" and "Grbl sender is using" in tool.error and not m.jogs,
          "a sender at work: refused in the machine's words, nothing moved")
    m.sender_refuse = None
    call(tool, "POST", "/on")
    drain(tool)
    check("refused" in call(tool, "POST", "/jog", {"x": 101}), "a jog past 100 mm is refused")
    check("refused" in call(tool, "POST", "/on"), "and on, while on")
    shutil.rmtree(d)

    print("a move that stops short")
    tool, m, c, d, wall = world()
    m.short = 0.5
    call(tool, "POST", "/on")
    drain(tool)
    check((m.x, m.y) == (100.0, 50.0) and tool.moved is None and tool.state == "off" and m.outs == [True, False],
          "a start that stops short goes back, and lets the sender in: %s %s" % ((m.x, m.y), m.outs))
    check("short" in tool.error, "and says so: %s" % tool.error)
    call(tool, "POST", "/on")
    drain(tool)
    m.short = 0.4
    call(tool, "POST", "/off")
    drain(tool)
    check(tool.state == "on" and tool.moved == (14.76, 0.0) and m.kept_out,
          "a return that stops short keeps the tool in use, the sender out, and the distance left: %s" % (tool.moved,))
    call(tool, "POST", "/off")
    drain(tool)
    check(abs(m.x - 100.0) < 1e-6 and tool.state == "off" and not m.kept_out, "Done again ends on the spot")
    m.jog_refuse = "the machine is not idle"
    call(tool, "POST", "/on")
    drain(tool)
    check(tool.state == "off" and not m.kept_out and (m.x, m.y) == (100.0, 50.0),
          "a start whose move is refused lets the sender in and moves nothing")
    m.jog_refuse = None
    shutil.rmtree(d)

    print("the page's silence, and a freeze")
    tool, m, c, d, wall = world()
    call(tool, "POST", "/on")
    drain(tool)
    c.t += run.BEAT_S - 1
    tool.turn()
    check(tool.state == "on", "under the bound, the tool stays in use")
    tool.last_turn = c.t
    c.t += 30.0                         # the service was frozen for a job
    tool.turn()
    check(tool.state == "on" and (m.x, m.y) == (124.6, 50.0), "a freeze is not the page's silence")
    c.t += run.BEAT_S + 1
    tool.last_turn = c.t - 0.5
    tool.turn()
    check(tool.state == "off" and (m.x, m.y) == (100.0, 50.0) and not m.kept_out and "page closed" in tool.message,
          "the page's silence ends it: the head back, the sender in: %s" % tool.message)
    shutil.rmtree(d)

    print("the operator lets the sender back in")
    tool, m, c, d, wall = world()
    call(tool, "POST", "/on")
    drain(tool)
    m.released = True
    c.t += run.RELEASE_POLL_S + 0.1
    tool.last_beat = tool.last_turn = c.t
    tool.turn()
    check(tool.state == "off" and (m.x, m.y) == (100.0, 50.0) and m.outs[-1] is False and "You let" in tool.message,
          "the head moves back and the claim is ended: %s" % tool.message)
    shutil.rmtree(d)

    print("a service that restarts in the middle of a use")
    tool, m, c, d, wall = world()
    call(tool, "POST", "/on")
    drain(tool)
    m.outs.clear()
    again = run.Tool(m, d, clock=c, wall=lambda: wall[0] + 5, sleep=c.sleep)
    again.resume()
    check(again.state == "off" and (m.x, m.y) == (100.0, 50.0) and m.outs == [True, False]
          and not os.path.exists(os.path.join(d, "tool.json")) and "left in use" in again.message,
          "it finishes it: the sender out for the move, the head back, the sender in: %s" % m.outs)
    fresh = run.Tool(m, d, clock=c, wall=lambda: wall[0], sleep=c.sleep)
    fresh.offset_now()
    call(fresh, "POST", "/on")
    drain(fresh)
    check(os.path.exists(os.path.join(d, "tool.json")), "a use written down")
    m.jogs.clear()
    old = run.Tool(m, d, clock=c, wall=lambda: wall[0] + run.RESUME_S + 60, sleep=c.sleep)
    old.resume()
    check(not m.jogs and not os.path.exists(os.path.join(d, "tool.json")) and "not moved back" in old.message,
          "an old use is not resumed: nothing moves, and the page is told: %s" % old.message)
    shutil.rmtree(d)

    print("calibration")
    tool, m, c, d, wall = world()
    call(tool, "POST", "/calibrate/burn")
    drain(tool)
    check(tool.state == "burning" and m.kept_out and len(m.programs) == 1 and "M4 S200" in m.programs[0]
          and tool.fire == (100.0, 50.0), "the mark burns where the laser is, with the sender out")
    call(tool, "POST", "/calibrate/burned", {"ok": True})
    drain(tool)
    check(tool.state == "calibrating" and (m.x, m.y) == (124.6, 50.0), "the mark shown: the head moved by the offset")
    call(tool, "POST", "/jog", {"x": 0.3, "y": -0.2})
    drain(tool)
    call(tool, "POST", "/calibrate/save")
    drain(tool)
    check(m.cfg["offset_x_mm"] == 24.9 and m.cfg["offset_y_mm"] == -0.2 and m.cfg["calibrated"] is True,
          "the head's move from the mark is the new offset: %s" % {k: m.cfg[k] for k in ("offset_x_mm", "offset_y_mm")})
    check(tool.state == "on" and tool.moved == (24.9, -0.2), "and the tool is in use from the mark")
    call(tool, "POST", "/off")
    drain(tool)
    check((m.x, m.y) == (100.0, 50.0) and not m.kept_out, "Done puts the laser on the mark")
    call(tool, "POST", "/calibrate/burn")
    drain(tool)
    call(tool, "POST", "/calibrate/burned", {"ok": False})
    drain(tool)
    check(tool.state == "off" and not m.kept_out and (m.x, m.y) == (100.0, 50.0), "a failed burn: nothing moved, the sender in")
    shutil.rmtree(d)

    print("PASS: service_test" if not failures else "FAIL: service_test, %d failure(s)" % failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
