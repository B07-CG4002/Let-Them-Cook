"""Queue deadlines without the Python 3.11 wait_for cancellation race."""
import asyncio

async def queue_item(queue,timeout=3):
    # The timeout runs in the current task: no extra child-task completion race.
    async with asyncio.timeout(timeout):
        return await queue.get()
