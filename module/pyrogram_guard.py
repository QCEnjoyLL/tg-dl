"""Work around the bundled Pyrogram fork's silent truncated downloads.

``tangyoha/pyrogram``'s ``Client.get_file`` catches every non-FloodWait
exception, only logs it, and then ends the chunk stream quietly. The downloader
therefore sees a stream that stops early: before this module existed it either
accepted the partial ``.temp`` file (``downloaded with wrong size``) or, once
``handle_download`` started deleting it, only got ``None`` back and reported a
generic "download aborted before the file was written".

The wrapper below keeps the stream honest: when the generator ends before
``file_size`` bytes were produced, it reopens the stream at the last chunk
boundary with a ``GetFile`` offset and keeps appending. Only after repeated
zero-progress attempts, or immediately when Telegram answers with a server-side
5xx, does it raise ``DownloadIncomplete``, so the downloader can retry or fail
with a clear reason instead of silently accepting a partial file.

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
import os
import re
import shutil
from typing import Optional

log = logging.getLogger(__name__)

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

#: Error types registered by other modules that must also propagate out of a
#: download stream (e.g. the Web UI's ``TaskCancelled``). Registered lazily to
#: avoid importing the downloader from this low-level module.
_EXTRA_PROPAGATED: list = []


def register_propagated_error(error_type) -> None:
    """Let ``error_type`` escape the stream instead of being swallowed.

    Without this a "delete this task" request was caught by the download
    wrappers, turned into a generic failure and retried three times before the
    task finalizer noticed the cancellation.
    """
    # pylint: disable = W0603
    global _PROPAGATED_ERRORS
    if error_type in _EXTRA_PROPAGATED:
        return
    _EXTRA_PROPAGATED.append(error_type)
    # Force the next lookup to rebuild the cached tuple.
    _PROPAGATED_ERRORS = ()


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
    _PROPAGATED_ERRORS = tuple(errors) + tuple(_EXTRA_PROPAGATED)
    return _PROPAGATED_ERRORS


def propagated_errors() -> tuple:
    """Errors the download path re-raises instead of swallowing."""
    return _propagated_errors()


#: Telegram answers ``FLOOD_PREMIUM_WAIT`` when a free account makes too many
#: GetFile requests ("wait N seconds, or buy Premium"). Waiting inside the
#: stream keeps the bytes already on disk; letting the error escape made
#: ``download_media`` restart a multi-GB file from zero.
_FLOOD_ERRORS: tuple = ()

#: Consecutive flood waits per stream before the failure is reported upwards
#: (``download_media`` then backs off and retries the whole file).
MAX_FLOOD_WAITS_PER_STREAM = 20

#: Extra second added to the wait Telegram asks for (tests set this to 0).
FLOOD_WAIT_MARGIN = 1.0


def _flood_errors() -> tuple:
    """Flood errors that should be waited out instead of aborting the stream."""
    # pylint: disable = W0603
    global _FLOOD_ERRORS
    if _FLOOD_ERRORS:
        return _FLOOD_ERRORS
    import pyrogram  # pylint: disable = C0415

    errors = []
    for name in ("FloodWait", "FloodPremiumWait"):
        error = getattr(pyrogram.errors, name, None)
        if isinstance(error, type):
            errors.append(error)
    _FLOOD_ERRORS = tuple(errors)
    return _FLOOD_ERRORS


def flood_wait_seconds(error: BaseException) -> float:
    """Seconds Telegram asks the client to wait before repeating the request."""
    try:
        return max(float(getattr(error, "value", 0) or 0), 0.0)
    except (TypeError, ValueError):
        return 0.0


def flood_errors() -> tuple:
    """Flood errors that should be waited out instead of aborting a stream."""
    return _flood_errors()


def record_swallowed_error(error: BaseException) -> None:
    """Remember an error the download path hid, for ``handle_download``'s caller.

    Used by the parallel downloader (``module.fast_download``), which cleans up
    after itself and returns ``None`` exactly like the stock implementation.
    """
    _captured_error.set(error)


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
        # Only whole-file calls are guarded. A ``limit`` means "fetch this range"
        # (the parallel downloader), which keeps the fork's behaviour; a plain
        # ``offset`` now means "resume this file from that chunk", which the
        # resumable ``handle_download`` uses to continue a partial ``.temp``.
        if not file_size or limit:
            async for chunk in original(
                self, file_id, file_size, limit, offset, progress, progress_args
            ):
                yield chunk
            return

        received = max(offset, 0) * CHUNK_SIZE
        stalled = 0
        flood_waits = 0
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
                    flood_waits = 0
                    yield chunk
                    if received >= file_size:
                        break
            except _flood_errors() as flood:
                # Telegram throttles free accounts with FLOOD_PREMIUM_WAIT;
                # sleeping here keeps the partial file instead of restarting it.
                wait = flood_wait_seconds(flood)
                flood_waits += 1
                if flood_waits > MAX_FLOOD_WAITS_PER_STREAM:
                    _raise_incomplete(received, file_size, cause=flood)
                log.warning(
                    "[%s] flood wait %ss on GetFile (%s); resuming at %s/%s bytes",
                    getattr(self, "name", "client"),
                    wait,
                    type(flood).__name__,
                    received,
                    file_size,
                )
                await asyncio.sleep(wait + FLOOD_WAIT_MARGIN)
                continue
            except _propagated_errors():
                # StopTransmission must reach ``download_media``.
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


def resumable_handle_download(original):
    """Keep a partial ``.temp`` so the next attempt continues where it stopped.

    Stock ``handle_download`` opens the target with ``"wb"`` and deletes the
    partial file whenever ``get_file`` fails, so a 2 GB download that dies at
    90% starts again from zero on the retry — or after a restart, which is the
    common case when Telegram throttles an account for hours. This wrapper keeps
    the bytes, resumes at the last complete 1 MiB chunk and renames on success.
    """

    async def handle_download(self, packet):
        file_id, directory, file_name, in_memory, file_size, progress, progress_args = (
            packet
        )
        if in_memory or not file_size:
            return await original(self, packet)

        os.makedirs(directory, exist_ok=True)
        temp_path = (
            os.path.abspath(re.sub("\\\\", "/", os.path.join(directory, file_name)))
            + ".temp"
        )
        final_path = os.path.splitext(temp_path)[0]

        start_offset = _resume_offset(temp_path, file_size)
        if start_offset >= file_size:
            # A complete leftover from a run that died before the rename.
            shutil.move(temp_path, final_path)
            return final_path

        received = start_offset
        try:
            with open(temp_path, "ab" if start_offset else "wb") as file_obj:
                async for chunk in self.get_file(
                    file_id,
                    file_size,
                    0,
                    start_offset // CHUNK_SIZE,
                    progress,
                    progress_args,
                ):
                    if not chunk:
                        continue
                    file_obj.write(chunk)
                    received += len(chunk)
        except BaseException as error:  # pylint: disable = W0703
            if isinstance(error, (asyncio.CancelledError,) + _propagated_errors()):
                raise
            _captured_error.set(error)
            log.warning(
                "[%s] download failed after %s/%s bytes; keeping the partial file",
                getattr(self, "name", "client"),
                received,
                file_size,
            )
            return None

        if received < file_size:
            _captured_error.set(DownloadIncomplete(received, file_size))
            return None

        shutil.move(temp_path, final_path)
        return final_path

    handle_download.__tdl_resumable__ = True
    return handle_download


def _resume_offset(temp_path: str, file_size: int) -> int:
    """Bytes already on disk that can be trusted, or 0 to start over."""
    if not os.path.exists(temp_path):
        return 0
    try:
        written = os.path.getsize(temp_path)
    except OSError:
        return 0
    if written <= 0:
        return 0
    if written >= file_size:
        return file_size
    # Resume on a chunk boundary: a stream can stop in the middle of a chunk,
    # and appending from a partial chunk would duplicate bytes.
    aligned = (written // CHUNK_SIZE) * CHUNK_SIZE
    if aligned != written:
        try:
            with open(temp_path, "r+b") as file_obj:
                file_obj.truncate(aligned)
        except OSError:
            return 0
    return aligned


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
        # Composed outside-in: the guard turns a swallowed failure into an
        # exception, the parallel downloader sits between the guard and the
        # stock implementation so it can reuse the same cleanup contract.
        # Imported here because fast_download imports this module.
        from module.fast_download import (  # pylint: disable = C0415
            parallel_handle_download,
        )

        pyrogram.Client.handle_download = guard_handle_download(
            parallel_handle_download(original_handle_download)
        )
        installed = True

    return installed
