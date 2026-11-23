"""Tests for running cloud uploads outside the download workers."""

import asyncio
import unittest

from module import upload_dispatch
from module.upload_dispatch import UploadDispatcher


class UploadDispatcherTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        upload_dispatch.set_limit(0)

    def tearDown(self):
        upload_dispatch.set_limit(0)

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

    async def test_limit_serialises_uploads(self):
        upload_dispatch.set_limit(1)
        order = []
        release = asyncio.Event()

        async def first():
            order.append("first-start")
            await release.wait()
            order.append("first-end")

        async def second():
            order.append("second-start")

        dispatcher = UploadDispatcher()
        dispatcher.submit(first)
        dispatcher.submit(second)
        # Both are submitted (one running, one queued), but only one may start.
        self.assertEqual(dispatcher.pending, 2)
        await asyncio.sleep(0.05)
        self.assertEqual(order, ["first-start"])

        release.set()
        await dispatcher.drain(timeout=2)
        self.assertEqual(order, ["first-start", "first-end", "second-start"])

    async def test_raising_the_limit_lets_queued_jobs_start(self):
        upload_dispatch.set_limit(1)
        order = []
        release = asyncio.Event()

        async def blocked():
            order.append("blocked-start")
            await release.wait()

        async def quick():
            order.append("quick")

        dispatcher = UploadDispatcher()
        dispatcher.submit(blocked)
        dispatcher.submit(quick)
        await asyncio.sleep(0.05)
        self.assertEqual(order, ["blocked-start"])

        upload_dispatch.set_limit(2)
        dispatcher.submit(quick)
        await asyncio.sleep(0.05)
        # New submissions build a fresh semaphore with the new limit.
        self.assertEqual(order, ["blocked-start", "quick"])

        release.set()
        await dispatcher.drain(timeout=2)
        self.assertEqual(dispatcher.pending, 0)

    def test_set_limit_parses_garbage_as_unlimited(self):
        self.assertEqual(upload_dispatch.set_limit("many"), 0)
        self.assertEqual(upload_dispatch.set_limit(-3), 0)
        self.assertEqual(upload_dispatch.set_limit(None), 0)
        self.assertEqual(upload_dispatch.set_limit("4"), 4)
        self.assertEqual(upload_dispatch.get_limit(), 4)
