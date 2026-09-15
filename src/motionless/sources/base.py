"""The motion source interface.

A source is a small object that produces :class:`~motionless.motion.MotionSample`
values. Sources run on their own thread and hand samples to a callback, which
keeps the GTK main loop free of blocking reads.
"""

from __future__ import annotations

import abc
import threading
from collections.abc import Callable
from dataclasses import dataclass

from motionless.motion import MotionSample

#: Called with every sample a source produces. Must be cheap and thread-safe.
SampleCallback = Callable[[MotionSample], None]


@dataclass(frozen=True)
class Availability:
    """Whether a source can run here, and why not when it cannot."""

    available: bool
    detail: str = ""

    def __bool__(self) -> bool:
        return self.available

    @classmethod
    def ok(cls, detail: str = "") -> Availability:
        return cls(True, detail)

    @classmethod
    def no(cls, detail: str) -> Availability:
        return cls(False, detail)


class MotionSource(abc.ABC):
    """Base class for motion sources.

    Subclasses implement :meth:`_run`, a loop that calls ``self.emit(sample)``
    until :attr:`stopping` is set.
    """

    #: Short identifier used in configuration and on the command line.
    name: str = "base"
    #: One-line description shown by ``motionless sources``.
    description: str = ""

    def __init__(self, on_sample: SampleCallback) -> None:
        self._on_sample = on_sample
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._error: BaseException | None = None

    # ------------------------------------------------------------- discovery

    @classmethod
    def probe(cls) -> Availability:
        """Report whether this source could run on this machine right now."""
        return Availability.ok()

    # ------------------------------------------------------------- lifecycle

    @property
    def stopping(self) -> bool:
        """Whether :meth:`stop` has been called. Use as a loop condition."""
        return self._stop.is_set()

    def stop_requested(self) -> bool:
        """Re-read the stop flag.

        Inside a ``while not self.stopping`` loop, static analysis reasonably
        assumes the property stays false. Another thread may have set it since,
        so anywhere that matters — notably an exception handler for a read that
        :meth:`stop` interrupted on purpose — asks again through this method.
        """
        return self._stop.is_set()

    @property
    def error(self) -> BaseException | None:
        """The exception that ended the source's thread, if any."""
        return self._error

    def details(self) -> dict[str, object]:
        """Extra facts for ``motionless status``, if the source has any.

        Sources that a user has to interact with — a phone to pair, a sender to
        point somewhere — answer here so ``status`` can show the address rather
        than make them go looking for it.
        """
        return {}

    def emit(self, sample: MotionSample) -> None:
        self._on_sample(sample)

    def wait(self, seconds: float) -> bool:
        """Sleep, returning ``True`` if a stop was requested meanwhile."""
        return self._stop.wait(seconds)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError(f"{self.name} source already started")
        self._stop.clear()
        self._error = None
        self._thread = threading.Thread(target=self._guarded_run, name=f"motion-{self.name}")
        self._thread.daemon = True
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self.wake()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)

    def wake(self) -> None:  # noqa: B027 - an optional hook, not a requirement
        """Unblock a source parked in a blocking read.

        Sources that block in a syscall (a socket read, say) override this
        so :meth:`stop` does not have to wait for a timeout. Polling
        sources need nothing here.
        """

    def _guarded_run(self) -> None:
        try:
            self._run()
        except Exception as exc:
            self._error = exc

    @abc.abstractmethod
    def _run(self) -> None:
        """Produce samples until :attr:`stopping` becomes true."""
