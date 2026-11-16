"""Regression tests for the paged chat-history iterator.

An empty page is how ``messages.GetHistory`` says "no more messages". Recovering
from it through ``Client.get_chat_history`` restarted a reverse scan from the end
of the chat, so a full-history task never finished and its progress card never
appeared.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from module import get_chat_history_v2 as history


def make_message(message_id: int):
    return SimpleNamespace(id=message_id, empty=False)


class PagedHistory:
    """``get_chunk_v2`` stand-in that serves ascending or descending pages."""

    def __init__(self, ids, reverse: bool, transient_empty: int = 0):
        self.ids = list(ids)
        self.reverse = reverse
        self.transient_empty = transient_empty
        self.calls = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.transient_empty:
            self.transient_empty -= 1
            return []

        limit = kwargs["limit"]
        start = kwargs["from_message_id"]
        if not start:
            # What GetHistory does with a zero offset: start at the newest
            # message (forward) or at the oldest one (reverse).
            start = (
                (self.ids[0] if self.ids else 0)
                if self.reverse
                else max(self.ids, default=0) + 1
            )
        if self.reverse:
            # The iterator continues at last_id + 1, so this end is inclusive.
            candidates = [i for i in self.ids if i >= start]
        else:
            # GetHistory treats offset_id as exclusive when paging downwards.
            candidates = [i for i in reversed(self.ids) if i < start]
        return [make_message(i) for i in candidates[:limit]]


class FakeClient:
    """Stock ``get_chat_history`` returns newest-first and ignores ordering."""

    def __init__(self, ids):
        self.ids = list(ids)
        self.history_calls = 0

    async def get_chat_history(self, chat_id, limit=0, offset=0, offset_id=0):
        self.history_calls += 1
        for message_id in reversed(self.ids):
            yield make_message(message_id)


class GetChatHistoryTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = mock.patch.object(history, "EMPTY_PAGE_RETRY_SLEEP", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def collect(self, client, pages, **kwargs):
        with mock.patch.object(history, "get_chunk_v2", pages):
            return [
                message.id
                async for message in history.get_chat_history_v2(
                    client, "chat", **kwargs
                )
            ]

    async def test_reverse_scan_stops_at_the_end_of_the_history(self):
        ids = list(range(389, 1001))
        client = FakeClient(ids)
        pages = PagedHistory(ids, reverse=True)

        yielded = await self.collect(
            client, pages, limit=0, offset_id=389, reverse=True
        )

        self.assertEqual(yielded, ids)
        # The stock iterator would hand back the newest messages again.
        self.assertEqual(client.history_calls, 0)

    async def test_forward_scan_stops_at_the_beginning(self):
        ids = list(range(1, 401))
        client = FakeClient(ids)
        pages = PagedHistory(ids, reverse=False)

        yielded = await self.collect(client, pages, limit=0, reverse=False)

        self.assertEqual(yielded, list(reversed(ids)))
        self.assertEqual(client.history_calls, 0)

    async def test_limit_stops_the_iteration(self):
        ids = list(range(1, 401))
        pages = PagedHistory(ids, reverse=True)

        yielded = await self.collect(
            FakeClient(ids), pages, limit=5, offset_id=1, reverse=True
        )

        self.assertEqual(yielded, [1, 2, 3, 4, 5])

    async def test_transient_empty_page_is_retried(self):
        ids = list(range(1, 251))
        pages = PagedHistory(ids, reverse=True, transient_empty=1)

        yielded = await self.collect(
            FakeClient(ids), pages, limit=0, offset_id=1, reverse=True
        )

        # The empty page was retried and the scan continued from page two.
        self.assertEqual(yielded, ids)

    async def test_empty_chat_terminates(self):
        client = FakeClient([])
        pages = PagedHistory([], reverse=True)

        yielded = await self.collect(client, pages, limit=0, reverse=True)

        self.assertEqual(yielded, [])
        self.assertEqual(len(pages.calls), 2)
        self.assertEqual(client.history_calls, 0)


if __name__ == "__main__":
    unittest.main()
