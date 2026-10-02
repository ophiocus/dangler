"""Marshal bridge requests onto Krita's GUI thread.

libkis is a set of Qt slots over KisImage/KisNode and is only safe on the thread that owns
them. The HTTP listener runs on a worker thread and must never touch Krita; it enqueues a
Job and waits with a deadline. A single repeating QTimer on the GUI thread drains the queue
(one persistent timer, not one per request: the per-request variant lags and reorders).

Two lessons paid for by earlier bridges are baked in here:
  * results and errors cross threads as plain JSON-able data and *strings* — never a
    Document/Node wrapper, which turns the next GC into a use-after-free once Krita has
    destroyed the C++ object underneath;
  * a request whose deadline passes is answered ``krita_busy`` and marked cancelled, so a
    modal dialog or a long filter never leaves the client hanging.
"""
import queue
import threading
import time
import traceback

from .qtcompat import QTimer


class Job(object):
    __slots__ = ("cmd", "args", "done", "ok", "result", "error", "trace", "cancelled", "queued_at")

    def __init__(self, cmd, args):
        self.cmd = cmd
        self.args = args
        self.done = threading.Event()
        self.ok = False
        self.result = None
        self.error = None
        self.trace = None
        self.cancelled = False
        self.queued_at = time.time()


class Invoker(object):
    """Owns the queue and the GUI-thread timer. Construct on the GUI thread."""

    def __init__(self, commands, log, interval_ms=20, max_per_tick=4):
        self._commands = commands          # dict name -> callable(args) -> jsonable
        self._log = log
        self._q = queue.Queue()
        self._max = max_per_tick
        self._timer = QTimer()
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._drain)
        self._busy = False

    def start(self):
        self._timer.start()

    def stop(self):
        self._timer.stop()

    # ---- worker-thread side -------------------------------------------------------
    def call(self, cmd, args, timeout):
        """Run `cmd` on the GUI thread; block up to `timeout` seconds. Returns a Job."""
        job = Job(cmd, args)
        self._q.put(job)
        if not job.done.wait(timeout):
            job.cancelled = True
            job.ok = False
            job.error = ("krita_busy: Krita's GUI thread did not run '%s' within %.0fs "
                         "(a modal dialog open, a long operation running, or Krita is hung)" % (cmd, timeout))
        return job

    # ---- GUI-thread side ----------------------------------------------------------
    def _drain(self):
        if self._busy:                      # re-entrancy guard: a command pumped the event loop
            return
        self._busy = True
        try:
            for _ in range(self._max):
                try:
                    job = self._q.get_nowait()
                except queue.Empty:
                    return
                if job.cancelled:
                    continue
                self._run(job)
        finally:
            self._busy = False

    def _run(self, job):
        fn = self._commands.get(job.cmd)
        try:
            if fn is None:
                raise KeyError("unknown command '%s'" % job.cmd)
            result = fn(job.args or {})
            job.result = result
            job.ok = True
        except Exception as e:  # noqa: BLE001 — everything must be caught: an unhandled exception raises Krita's modal excepthook
            job.ok = False
            job.error = "%s: %s" % (type(e).__name__, e)
            job.trace = traceback.format_exc()
            self._log("command %s failed: %s" % (job.cmd, job.error))
        finally:
            job.done.set()
