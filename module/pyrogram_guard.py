"""Work around the bundled Pyrogram fork's silent truncated downloads.

``tangyoha/pyrogram``'s ``Client.get_file`` catches every non-FloodWait
exception and only logs it, so a dropped ``upload.GetFile`` request ends the
chunk stream quietly. ``handle_download`` then moves the partial ``.temp`` file
into place and returns it as if the download had succeeded -- the caller only
notices through a size check. That is why a stalled 210 MB transfer appeared as
"downloaded with wrong size: 5242880" and was then reported as an expired file
reference.

The wrapper below keeps the stream honest: when the generator ends before
``file_size`` bytes were produced, it reopens the stream at the last chunk
boundary with a ``GetFile`` offset and keeps appending. Only after repeated
zero-progress attempts does it raise ``DownloadIncomplete``, so the downloader
can retry or fail with a clear reason instead of silently accepting a partial
file.

Two things are added on top of the bare resume logic:

* the exception the fork logged right before ending the stream is captured and
  attached to ``DownloadIncomplete``, so the failure reason says
  ``Timeout: Telegram says: [-500 TIMEOUT] ...`` instead of a generic
  "download aborted before the file was written";
* ``handle_download`` is patched to re-raise ``DownloadIncomplete``. Stock
  Pyrogram swallows every ``BaseException`` raised by ``get_file`` and returns
  ``None``, which used to hide the real protocol error from the downloader.
"""

import asyncio
import contextvars
import functools
import logging
from typing import Optional

#: Telegram's ``upload.GetFile`` returns at most 1 MiB per request.
CHUNK_SIZE = 1024 * 1024

#: Consecutive attempts that made no progress at all before giving up.
MAX_STALLED_ATTEMPTS = 3

#: Pause between resume attempts (tests override this).
RESUME_SLEEP = 1.0

#: The most recent error the fork swallowed, scoped to the current asyncio task.
#: The logging handler below writes into this context variable; concurrent
#: downloads each get their own copy, so one failure cannot be attributed to
#: another task.
_captured_error: contextvars.ContextVar[Optional[BaseException]] = (
    contextvars.ContextVar("tdl_captured_download_error", default=None)
)

#: Errors the fork deliberately re-raises. The guard must not turn them into
#: ``DownloadIncomplete``, or ``download_media``'s FloodWait handling breaks.
_PROPAGATED_ERRORS: tuple = ()


def _propagated_errors() -> tuple:
    """Build the tuple of errors Pyrogram re-raises out of ``get_file``."""
    # pylint: disable = W0603
    global _PROPAGATED_ERRORS
    if _PROPAGATED_ERRORS:
        return _PROPAGATED_ERRORS
    import pyrogram  # pylint: disable = C0415

    errors = [pyrogram.StopTransmission]
    for name in ("FloodWait", "FloodPremiumWait"):
        error = getattr(pyrogram.errors, name, None)
        if isinstance(error, type):
            errors.append(error)
    _PROPAGATED_ERRORS = tuple(errors)
    return _PROPAGATED_ERRORS


class DownloadIncomplete(Exception):
    """The file stream ended before the announced size was received."""

    def __init__(
        self,
        received: int,
        expected: int,
        cause: Optional[BaseException] = None,
    ):
        super().__init__(f"only received {received} of {expected} bytes")
        self.received = received
        self.expected = expected
        #: The exception the fork logged before the stream died, if any.
        self.cause = cause

    def __str__(self) -> str:
        text = super().__str__()
        if self.cause is not None:
            text = f"{text} ({type(self.cause).__name__}: {self.cause})"
        return text


class _SwallowedErrorHandler(logging.Handler):
    """Record the exception the fork logs right before it ends a stream.

    ``get_file`` ends with ``except Exception as e: log.exception(e)``, so an
    error record on the ``pyrogram`` logger is the only trace left of the
    request that failed. Logging runs synchronously inside the downloading
    task, which makes a context variable the right place to keep it.
    """

    def __init__(self):
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        explicit = record.exc_info[1] if record.exc_info else None
        if explicit is not None:
            _captured_error.set(explicit)
        elif record.levelno >= logging.ERROR:
            _captured_error.set(RuntimeError(record.getMessage()))


_capture_handler_installed = False


def _install_error_capture() -> None:
    """Attach the capture handler to the ``pyrogram`` logger exactly once."""
    # pylint: disable = W0603
    global _capture_handler_installed
    if _capture_handler_installed:
        return
    logger = logging.getLogger("pyrogram")
    # The emitting logger's effective level gates ``Logger.handle`` before any
    # handler sees the record; keep it at WARNING (what the downloader already
    # does) so the fork's ``log.exception(e)`` is not filtered out first.
    if logger.getEffectiveLevel() > logging.WARNING:
        logger.setLevel(logging.WARNING)
    logger.addHandler(_SwallowedErrorHandler())
    _capture_handler_installed = True


def _is_server_error(exc: Optional[BaseException]) -> bool:
    """Whether ``exc`` is a Telegram-side 5xx error.

    ``[-500 TIMEOUT]`` on ``upload.GetFile`` means the DC worker could not read
    the file; retrying the same offset just waits for another timeout, so the
    guard should surface it instead of burning through its resume attempts.
    """
    if exc is None:
        return False
    try:
        import pyrogram  # pylint: disable = C0415

        if isinstance(exc, pyrogram.errors.RPCError):
            code = getattr(exc, "CODE", None)
            return isinstance(code, int) and code >= 500
    except Exception:  # pylint: disable = W0703
        pass
    return False


def _raise_incomplete(
    received: int, expected: int, cause: Optional[BaseException] = None
) -> None:
    """Raise ``DownloadIncomplete`` and let ``handle_download`` see the cause."""
    incomplete = DownloadIncomplete(received, expected, cause=cause)
    _captured_error.set(incomplete)
    raise incomplete


def guard_get_file(original):
    """Wrap ``Client.get_file`` with resume-on-truncation semantics."""

    @functools.wraps(original)
    async def guarded_get_file(
        self,
        file_id,
        file_size: int = 0,
        limit: int = 0,
        offset: int = 0,
        progress=None,
        progress_args=(),
    ):
        # Only the plain "download the whole file" call is guarded. Callers that
        # ask for a limited range keep the fork's behaviour.
        if not file_size or limit or offset:
            async for chunk in original(
                self, file_id, file_size, limit, offset, progress, progress_args
            ):
                yield chunk
            return

        received = 0
        stalled = 0
        while received < file_size:
            start_chunk = received // CHUNK_SIZE
            skip = received - start_chunk * CHUNK_SIZE
            progressed = False
            _captured_error.set(None)

            try:
                async for chunk in original(
                    self, file_id, file_size, 0, start_chunk, progress, progress_args
                ):
                    if skip:
                        if len(chunk) <= skip:
                            skip -= len(chunk)
                            continue
                        chunk = chunk[skip:]
                        skip = 0
                    if not chunk:
                        continue
                    received += len(chunk)
                    progressed = True
                    yield chunk
                    if received >= file_size:
                        break
            except _propagated_errors():
                # FloodWait / StopTransmission must reach ``download_media``.
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pylint: disable = W0703
                # A protocol error escaped the fork: fail with the cause
                # attached instead of resuming a stream that is not coming.
                _raise_incomplete(received, file_size, cause=exc)

            if received >= file_size:
                break

            cause = _captured_error.get()

            if _is_server_error(cause) and not progressed:
                # The DC worker is not serving this file (usually
                # ``[-500 TIMEOUT]``) and the attempt made no progress at all;
                # reopening the same offset would only wait for another
                # timeout. Let download_media retry with backoff.
                _raise_incomplete(received, file_size, cause=cause)

            if progressed:
                stalled = 0
            else:
                stalled += 1
                if stalled >= MAX_STALLED_ATTEMPTS:
                    _raise_incomplete(received, file_size, cause=cause)
            await asyncio.sleep(RESUME_SLEEP)

    guarded_get_file.__tdl_guarded__ = True
    return guarded_get_file


def guard_handle_download(original):
    """Re-raise ``DownloadIncomplete`` instead of letting Pyrogram hide it.

    Stock ``handle_download`` catches ``BaseException``, deletes the partial
    ``.temp`` file, and returns ``None``. The downloader then only sees "no path
    came back", which is why ``[-500 TIMEOUT]`` failures used to be reported as
    a generic interruption.
    """

    @functools.wraps(original)
    async def guarded_handle_download(self, packet):
        _captured_error.set(None)
        result = await original(self, packet)
        if result is None:
            captured = _captured_error.get()
            if isinstance(captured, DownloadIncomplete):
                raise captured
        return result

    guarded_handle_download.__tdl_guarded__ = True
    return guarded_handle_download


def install_get_file_guard() -> bool:
    """Install the guard once per process. Returns whether it was installed."""
    import pyrogram  # pylint: disable = C0415

    _install_error_capture()

    installed = False
    original_get_file = pyrogram.Client.get_file
    if not getattr(original_get_file, "__tdl_guarded__", False):
        pyrogram.Client.get_file = guard_get_file(original_get_file)
        installed = True

    original_handle_download = pyrogram.Client.handle_download
    if not getattr(original_handle_download, "__tdl_guarded__", False):
        pyrogram.Client.handle_download = guard_handle_download(
            original_handle_download
        )
        installed = True

    return installed
