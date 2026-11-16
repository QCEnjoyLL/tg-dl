"""Rewrite pyrogram.get_chat_history"""

import asyncio
from datetime import datetime
from typing import AsyncGenerator, Optional, Union

import pyrogram

# pylint: disable = W0611
from pyrogram import raw, types, utils

#: Pause before retrying a page that came back empty (tests set this to 0).
EMPTY_PAGE_RETRY_SLEEP = 1.0


async def get_chunk_v2(
    *,
    client: pyrogram.Client,
    chat_id: Union[int, str],
    limit: int = 0,
    offset: int = 0,
    max_id: int = 0,
    from_message_id: int = 0,
    from_date: datetime = utils.zero_datetime(),
    reverse: bool = False
):
    """get chunk"""
    from_message_id = from_message_id or (1 if reverse else 0)

    messages = await utils.parse_messages(
        client,
        await client.invoke(
            raw.functions.messages.GetHistory(
                peer=await client.resolve_peer(chat_id),
                offset_id=from_message_id,
                offset_date=utils.datetime_to_timestamp(from_date),
                add_offset=offset * (-1 if reverse else 1) - (limit if reverse else 0),
                limit=limit,
                max_id=max_id,
                min_id=0,
                hash=0,
            ),
            sleep_threshold=60,
        ),
        replies=0,
    )

    if reverse:
        messages.reverse()

    return messages


# pylint: disable = C0301
async def get_chat_history_v2(
    self: pyrogram.Client,
    chat_id: Union[int, str],
    limit: int = 0,
    max_id: int = 0,
    offset: int = 0,
    offset_id: int = 0,
    offset_date: datetime = utils.zero_datetime(),
    reverse: bool = False,
) -> Optional[AsyncGenerator["types.Message", None]]:
    """Get messages from a chat history."""
    current = 0
    total = limit or (1 << 31) - 1
    limit = min(100, total)
    # ``GetHistory`` returns an empty page once the chat has no more messages —
    # that is its only "end of history" signal, and an empty chat answers it
    # from the very first page. Retry once in case the page was empty because of
    # a transient failure, then stop.
    empty_retries = 0

    while True:
        messages = await get_chunk_v2(
            client=self,
            chat_id=chat_id,
            limit=limit,
            offset=offset,
            max_id=max_id + 1 if max_id else 0,
            from_message_id=offset_id,
            from_date=offset_date,
            reverse=reverse,
        )

        if not messages:
            # An empty page used to be "recovered" with Client.get_chat_history.
            # That API has no ``reverse`` support and returns the *newest*
            # messages, so a reverse scan restarted from the end of the chat and
            # never finished: the task's progress card never appeared because
            # ``is_running`` is only set once the scan completes.
            if empty_retries >= 1:
                return
            empty_retries += 1
            await asyncio.sleep(EMPTY_PAGE_RETRY_SLEEP)
            continue

        empty_retries = 0
        offset_id = messages[-1].id + (1 if reverse else 0)

        for message in messages:
            yield message

            current += 1

            if current >= total:
                return
