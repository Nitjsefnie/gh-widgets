"""Acquisition and cache-fallback observability shared by the renderers.

Plain sibling imports are intentional: the repository root is ``sys.path[0]``
in development and tests, while ``/usr/local/bin`` is ``sys.path[0]`` for each
deployed renderer.
"""
import contextlib
import re
from collections import namedtuple


# Control characters that XML 1.0 forbids outright — ESC among them, which is
# what a terminal reads as the start of an ANSI or OSC sequence. Stripped from
# anything that reaches a terminal (SVG text) or a journal line (CacheFallback).
# TAB, LF and CR are legal in XML and are handled where they matter.
_XML_FORBIDDEN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class CacheFallback(namedtuple("CacheFallback", "fetched_at phase error")):
    """A run drawing from cache because an acquisition failed.

    Every renderer's fallback prints one line, and that line is all an
    operator has to go on. It used to name neither the phase nor the cause
    (issue #55): three different failures with three different causes printed
    byte-identical output.

    ``fetched_at`` is the cache timestamp the cards are drawn from and are
    stamped with; ``phase`` names the acquisition function that raised, the
    same string the renderer calls it by in its own source.
    """

    __slots__ = ()

    @property
    def message(self):
        """The exception rendered as a single line: a GraphQL error carries a
        JSON body and can span several, and this is a journal summary."""
        # One line, and without the control characters a terminal acts on:
        # this is the first server-supplied text to reach a journal line.
        text = " ".join(_XML_FORBIDDEN.sub("", str(self.error)).split())
        return text or type(self.error).__name__


# The acquisition currently running, kept in a one-key dict rather than rebound
# through `global`. A renderer whose fetch entry point wraps SEVERAL
# acquisitions cannot hand its caller a phase name the way render.py's
# three flat calls do, and this is how the caller gets one anyway.
_LAST_ACQUISITION = {"phase": None}


@contextlib.contextmanager
def acquisition(phase):
    """Label whatever is raised inside with the acquisition it failed in.

    On success the previous label is restored. On failure the label STAYS, so
    the caller reading it once the exception has propagated names the right
    acquisition: an exception raised at the `yield` leaves the generator
    without running the line below. The exception itself is neither wrapped
    nor annotated — its type is what each main()'s handlers dispatch on (an
    HTTPError from gql must still reach the HTTP branch).
    """
    previous = _LAST_ACQUISITION["phase"]
    _LAST_ACQUISITION["phase"] = phase
    yield
    # Reached only when the body completed; a failure leaves the label set.
    _LAST_ACQUISITION["phase"] = previous


def take_last_acquisition():
    """The acquisition that failed, or None — and clear the label.

    Consumed rather than read, and on BOTH exits of a failed acquisition: a
    run that falls back is about to exit, and one that propagates has no line
    to put the name in. Either way a leftover would name this run's
    acquisition to the next one in the same process.
    """
    phase = _LAST_ACQUISITION["phase"]
    _LAST_ACQUISITION["phase"] = None
    return phase
