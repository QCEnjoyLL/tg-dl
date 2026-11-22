"""Tests for running cloud uploads outside the download workers."""

import asyncio
import unittest

from module.upload_dispatch import UploadDispatcher


class UploadDispatcherTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_jobs_run_concurrently(self):
        # Each job waits for the other: they can only both finish if the
        # dispatcher really starts them at the same time.
        started = asyncio.Event()
        other_started = asyncio.Event()
        done = []

        async def first():
            started.set()
            await asyncio.wait_for(other_started.wait(), timeout=1)
            done.append("first")

        async def second():
            other_started.set()
            await asyncio.wait_for(started.wait(), timeout=1)
            done.append("second")

        dispatcher = UploadDispatcher()
        dispatcher.submit(first)
        dispatcher.submit(second)

        self.assertEqual(dispatcher.pending, 2)
        dropped = await dispatcher.drain(timeout=2)

        self.assertEqual(dropped, 0)
        self.assertEqual(sorted(done), ["first", "second"])
        self.assertEqual(dispatcher.pending, 0)

    async def test_failing_job_is_contained(self):
        done = []

        async def boom():
            raise RuntimeError("drive offline")

        async def fine():
            done.append("ok")

        dispatcher = UploadDispatcher()
        dispatcher.submit(boom)
        dispatcher.submit(fine)

        dropped = await dispatcher.drain(timeout=1)

        self.assertEqual(dropped, 0)
        self.assertEqual(done, ["ok"])
        self.assertEqual(dispatcher.pending, 0)

    async def test_drain_cancels_when_the_timeout_expires(self):
        cancelled = []

        async def stuck():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        dispatcher = UploadDispatcher()
        dispatcher.submit(stuck)

        dropped = await dispatcher.drain(timeout=0.05)

        self.assertEqual(dropped, 1)
        self.assertEqual(cancelled, [True])
        self.assertEqual(dispatcher.pending, 0)

    async def test_drain_without_work_returns_immediately(self):
        self.assertEqual(await UploadDispatcher().drain(timeout=5), 0)