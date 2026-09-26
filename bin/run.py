#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# SPDX-License-Identifier: MIT
"""org.openglow.alignment's service: the alignment tool.

The head camera looks at the bed about 25 mm from where the laser lands
(offset_x_mm, offset_y_mm: calibrated per machine). While the tool is in
use, the head stands moved by that offset, so the camera's picture is of
the spot where the laser will land when the tool is done: the page shows
it with a crosshair, and the operator jogs, or slides the material, until
the crosshair is where the job starts. Done moves the head back by the
offset, and the laser is on that spot.

While the head stands moved, a job started from a Grbl sender would land
about 25 mm from where the operator put it. So the tool keeps the Grbl
sender out for as long as it is in use (sender.keep_out: the machine
disconnects the sender, and only on an idle machine), and lets it back in
once the head is back.

The service owns all of it, not the page. The page asks, and says it is
there: every call it makes counts. With no word from it for BEAT_S - the
browser closed, crashed, or went to another tab - the service ends the
tool itself: the head moves back, and the sender is let back in. The same
happens when the operator lets the sender back in from the panel, and a
service that restarts in the middle of a use finishes it. A time the
service was frozen (a job armed) does not count against the page.

`moved` is how far the head stands from where the laser is to land: the
offset, once the tool is in use. It is kept exact through a move that
stops short, so Done always takes the head back to the spot, and it is
written down (tool.json) before every move, so a service that restarts
knows it too.

The page's calls (ffx.service.call):

  GET  /state                     the tool as it stands (also the page's word that it is there)
  POST /on                        use the tool
  POST /off                       done: the head back, the sender back in
  POST /jog      {x, y}           move the head, while the tool is in use (mm)
  POST /cancel                    stop a jog
  POST /calibrate/burn            burn the calibration mark where the laser is (the tool not in use)
  POST /calibrate/burned {ok}     the burn is done (or failed): show the mark
  POST /calibrate/save            the mark is under the crosshair: the head's move is the new offset
  POST /calibrate/cancel          stop calibrating; the tool stays in use

Each answers the state. The moves happen on the service's own thread, so a
call never waits on one.
"""
import json
import os
import sys
import threading
import time

BEAT_S = 10.0           # no word from the page for this long ends the tool
FROZEN_GAP_S = 3.0      # a turn of the loop that took this long was a freeze
RESUME_S = 120.0        # a use interrupted longer ago than this is not resumed: the machine may have moved on
MOVE_FEED = 600.0       # mm/min: the tool's own moves, slow enough to watch (10 mm/s)
POS_TOL = 0.05          # mm: a move that ended this close to its target reached it
SETTLE_S = 60.0         # the longest a move may take to come to rest
RELEASE_POLL_S = 2.0    # how often the operator's release is looked for

MARK_DONE = "The laser is on the spot the crosshair marked. Start the job from the current position."


class Refused(Exception):
    """A refusal in words, for the page."""


class Machine:
    """The machine as the service reaches it: the SDK's calls. A test hands the tool its own."""

    def __init__(self, ffx):
        self.ffx = ffx

    def status(self):
        return self.ffx.machine.status()

    def jog(self, x, y, feed):
        return self.ffx.jog(x=round(x, 3), y=round(y, 3), feed=feed)

    def cancel(self):
        return self.ffx.cancel()

    def sender_out(self, out):
        return self.ffx.sender_out(out)

    def sender_state(self):
        return self.ffx.sender_state()

    def settings(self):
        return self.ffx.settings().get("settings", {})

    def set_settings(self, **patch):
        return self.ffx.set_settings(**patch)

    def job(self, program_text):
        name = self.ffx.write_program("mark.gcode", program_text)
        return self.ffx.job(name, lit_within_s=120, timeout_s=300)

    @staticmethod
    def words(e):
        return getattr(e, "words", None) or str(e)


def num(v, d):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else d


class Tool:
    def __init__(self, machine, data_dir, clock=time.monotonic, wall=time.time, sleep=time.sleep):
        self.m = machine
        self.path = os.path.join(data_dir, "tool.json")
        self.clock, self.wall, self.sleep = clock, wall, sleep
        self.lock = threading.Lock()
        self.wake = threading.Condition(self.lock)
        self.state = "off"              # off, starting, on, stopping, burning, calibrating
        self.message = ""               # what the page shows: the last thing that happened
        self.error = ""                 # a refusal, in the machine's words
        self.moved = None               # (x, y): how far the head stands from the spot, while in use
        self.fire = None                # where the calibration mark was burned
        self.cfg = {}                   # the package's settings
        self.jobs = []                  # commands for the worker
        self.last_beat = clock()
        self.last_turn = clock()
        self.last_release_check = 0.0

    # ---- the page's side -------------------------------------------------------------

    def snapshot(self):
        s = self.cfg
        return {"state": self.state, "message": self.message, "error": self.error,
                "offset": {"x": num(s.get("offset_x_mm"), 24.6), "y": num(s.get("offset_y_mm"), 0.0)},
                "calibrated": bool(s.get("calibrated")), "moved": list(self.moved) if self.moved else None}

    WANT = {"/on": ("off",), "/off": ("on", "calibrating"), "/jog": ("on", "calibrating"), "/cancel": None,
            "/calibrate/burn": ("off",), "/calibrate/burned": ("burning",), "/calibrate/save": ("calibrating",),
            "/calibrate/cancel": ("calibrating",)}

    def handle(self, method, path, body):
        with self.lock:
            self.last_beat = self.clock()
            if method == "GET" and path == "/state":
                return self.snapshot()
            if method != "POST" or path not in self.WANT:
                raise Refused("no such call")
            want = self.WANT[path]
            if want is not None and self.state not in want:
                raise Refused("not now: the tool is %s" % self.state)
            if path == "/jog":
                x, y = num(body.get("x"), 0.0), num(body.get("y"), 0.0)
                if abs(x) > 100 or abs(y) > 100 or (x == 0 and y == 0):
                    raise Refused("a jog is up to 100 mm in X and Y")
                self.jobs.append(("jog", (x, y)))
            elif path == "/calibrate/burned":
                self.jobs.append(("burned", bool(body.get("ok"))))
            else:
                self.jobs.append((path.strip("/").replace("/", "."), None))
            if path == "/on":
                self.state, self.error, self.message = "starting", "", ""
            elif path == "/off":
                self.state, self.error = "stopping", ""
            elif path != "/cancel":
                self.error = ""
            self.wake.notify()
            return self.snapshot()

    # ---- the record of a use the service may not live to finish -------------------------

    def remember(self):
        if not self.moved:
            return self.forget()
        tmp = self.path + ".new"
        with open(tmp, "w") as f:
            json.dump({"on": True, "moved": list(self.moved), "wall": self.wall()}, f)
        os.replace(tmp, self.path)

    def forget(self):
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    # ---- moves --------------------------------------------------------------------------

    def settle(self):
        """Wait for the head to come to rest; its position."""
        end = self.clock() + SETTLE_S
        last = None
        while self.clock() < end:
            st = self.m.status()
            pos = st.get("pos")
            if st.get("state") in ("idle", "disabled") and pos:
                here = (pos["x"], pos["y"])
                if last and abs(here[0] - last[0]) < 1e-3 and abs(here[1] - last[1]) < 1e-3:
                    return here
                last = here
            self.sleep(0.2)
        raise Refused("the head did not come to rest")

    def shift(self, dx, dy, feed):
        """Move the head by (dx, dy), keeping `moved` exact whatever happens: it is written down as if
        the move will reach its target, and put right from where the head really stops."""
        start = self.settle()
        before = self.moved or (0.0, 0.0)
        self.moved = (round(before[0] + dx, 3), round(before[1] + dy, 3))
        self.remember()
        err = None
        try:
            self.m.jog(dx, dy, feed)
        except Exception as e:
            err = e
        here = self.settle()
        self.moved = (round(before[0] + here[0] - start[0], 3), round(before[1] + here[1] - start[1], 3))
        if abs(self.moved[0]) < POS_TOL and abs(self.moved[1]) < POS_TOL:
            self.moved = None
        self.remember()
        if err is not None:
            raise err
        if abs(here[0] - start[0] - dx) > POS_TOL or abs(here[1] - start[1] - dy) > POS_TOL:
            raise Refused("the move stopped %.2f, %.2f mm short" % (start[0] + dx - here[0], start[1] + dy - here[1]))

    def home_to_spot(self, why=""):
        """The head back to the spot, then the sender back in, and the tool off."""
        if self.moved:
            self.shift(-self.moved[0], -self.moved[1], MOVE_FEED)
        self.fire = None
        self.forget()
        try:
            self.m.sender_out(False)
        except Exception as e:
            self.error = "the Grbl sender was not let back in: %s" % self.m.words(e)
        self.state = "off"
        self.message = why or MARK_DONE

    def offset_now(self):
        self.cfg = self.m.settings()
        return (num(self.cfg.get("offset_x_mm"), 24.6), num(self.cfg.get("offset_y_mm"), 0.0))

    def idle_or_refuse(self):
        st = self.m.status()
        if st.get("state") not in ("idle", "disabled") or not st.get("pos"):
            raise Refused("the machine is not idle")
        if st.get("motors_released"):
            raise Refused("the X and Y motors are released: energize them first")
        return st

    # ---- what the worker does -------------------------------------------------------------

    def turn_on(self):
        self.idle_or_refuse()
        self.m.sender_out(True)
        offset = self.offset_now()
        try:
            self.shift(offset[0], offset[1], MOVE_FEED)
        except Exception:
            self.back_out()
            raise
        self.state, self.message = "on", ""

    def back_out(self):
        """A start that failed: the head back where it was, and the sender in."""
        try:
            if self.moved:
                self.shift(-self.moved[0], -self.moved[1], MOVE_FEED)
        except Exception:
            pass
        if not self.moved:
            self.forget()
            try:
                self.m.sender_out(False)
            except Exception:
                pass

    def jog(self, x, y):
        feed = num(self.cfg.get("jog_feed"), 3000)
        self.m.jog(x, y, feed)
        self.settle()

    def burn(self):
        st = self.idle_or_refuse()
        self.m.sender_out(True)
        self.fire = (st["pos"]["x"], st["pos"]["y"])
        self.offset_now()
        s = self.cfg
        power = int(round(num(s.get("burn_power"), 20) * 10))
        feed = int(round(num(s.get("burn_feed"), 600)))
        program = "\n".join(["G21", "G91", "M4 S%d" % power, "G0 X-1.5", "G1 X3 F%d" % feed, "G0 X-1.5 Y-1.5",
                             "G1 Y3", "G0 Y-1.5", "M5", "G90", ""])
        try:
            self.m.job(program)
        except Exception:
            self.fire = None
            try:
                self.m.sender_out(False)
            except Exception:
                pass
            raise
        self.state = "burning"
        self.message = "Press the button on the machine when it lights. The mark burns where the laser is."

    def burned(self, ok):
        if not ok:
            self.fire = None
            self.state, self.message = "off", ""
            self.m.sender_out(False)
            return
        offset = self.offset_now()
        try:
            self.shift(offset[0], offset[1], MOVE_FEED)
        except Exception:
            self.fire = None
            self.back_out()
            raise
        self.state, self.message = "calibrating", ""

    def save(self):
        here = self.settle()
        ox, oy = round(here[0] - self.fire[0], 3), round(here[1] - self.fire[1], 3)
        self.m.set_settings(offset_x_mm=ox, offset_y_mm=oy, calibrated=True)
        self.offset_now()
        # The head stands moved from the mark by the new offset, which is what Done takes back.
        self.moved = (ox, oy)
        self.remember()
        self.fire = None
        self.state = "on"
        self.message = "Saved: the camera sees the bed %.2f mm, %.2f mm from the laser." % (ox, oy)

    def run_one(self, what, arg):
        try:
            if what == "on":
                self.turn_on()
            elif what == "off":
                self.home_to_spot()
            elif what == "jog":
                self.jog(*arg)
            elif what == "cancel":
                self.m.cancel()
            elif what == "calibrate.burn":
                self.burn()
            elif what == "burned":
                self.burned(arg)
            elif what == "calibrate.save":
                self.save()
            elif what == "calibrate.cancel":
                self.fire = None
                self.state = "on"
        except Exception as e:
            self.error = Machine.words(e)
            if what in ("on", "off", "calibrate.burn", "burned"):
                # In use exactly while the head stands moved from the spot.
                self.state = "on" if self.moved else "off"

    # ---- the loop ----------------------------------------------------------------------------

    def resume(self):
        """A use this service did not finish, from before it started."""
        self.offset_now()
        try:
            with open(self.path) as f:
                doc = json.load(f)
        except (OSError, ValueError):
            return
        age = self.wall() - num(doc.get("wall"), 0)
        moved = doc.get("moved")
        if not doc.get("on") or not isinstance(moved, list) or len(moved) != 2 or not 0 <= age <= RESUME_S:
            self.forget()
            if doc.get("on"):
                self.message = ("The tool was in use when it stopped, and the head was not moved back: check "
                                "where the laser is before a job.")
            return
        self.moved = (num(moved[0], 0.0), num(moved[1], 0.0))
        self.state = "stopping"
        try:
            try:
                self.m.sender_out(True)
            except Exception:
                pass
            self.home_to_spot("The tool was left in use: the head moved back, and the Grbl sender is let back in.")
        except Exception as e:
            self.error = Machine.words(e)
            self.state = "on" if self.moved else "off"

    def turn(self):
        """One turn: the page's commands, the page's silence, the operator's release."""
        with self.lock:
            now = self.clock()
            if now - self.last_turn > FROZEN_GAP_S:
                self.last_beat += now - self.last_turn     # frozen: the page's silence was not its own
            self.last_turn = now
            job = self.jobs.pop(0) if self.jobs else None
            silent = now - self.last_beat > BEAT_S
            state = self.state
        if job:
            self.run_one(*job)
            return
        if state in ("on", "calibrating", "burning") and silent:
            try:
                if self.moved:
                    self.home_to_spot("The page closed: the head moved back, and the Grbl sender is let back in.")
                else:
                    self.fire = None
                    self.state, self.message = "off", ""
                    self.m.sender_out(False)
            except Exception as e:
                self.error = Machine.words(e)
            return
        if state in ("on", "calibrating") and now - self.last_release_check > RELEASE_POLL_S:
            self.last_release_check = now
            try:
                released = self.m.sender_state().get("released")
            except Exception:
                released = False
            if released:
                try:
                    self.home_to_spot("You let the Grbl sender back in: the head moved back.")
                except Exception as e:
                    self.error = Machine.words(e)

    def run_forever(self):
        self.resume()
        while True:
            self.turn()
            with self.lock:
                if not self.jobs:
                    self.wake.wait(0.5)


def main():
    sys.path.insert(0, os.path.join(os.environ["FFX_PKG"], "lib"))
    import ffx

    tool = Tool(Machine(ffx), os.environ["FFX_DATA"])

    def answer(method, path, body):
        try:
            return tool.handle(method, path, body if isinstance(body, dict) else {})
        except Refused as e:
            raise ffx.CallError(409, str(e))

    ffx.serve(answer, background=True)
    tool.run_forever()


if __name__ == "__main__":
    main()
