"""Coordinate credential changes with already accepted API operations."""
import asyncio
from contextlib import asynccontextmanager


def github_id(value):
    if value is not None and (type(value) is not int or not 0 < value <= 9007199254740991):
        raise ValueError('owner_github_id must be a positive numeric GitHub ID or null')
    return value


class AccessGate:
    def __init__(self):
        self.condition = asyncio.Condition()
        self.active = 0
        self.changing = False

    @asynccontextmanager
    async def operation(self):
        async with self.condition:
            await self.condition.wait_for(lambda: not self.changing)
            self.active += 1
        try:
            yield
        finally:
            async with self.condition:
                self.active -= 1
                self.condition.notify_all()

    @asynccontextmanager
    async def change(self):
        async with self.condition:
            await self.condition.wait_for(lambda: not self.changing)
            self.changing = True
            try:
                await self.condition.wait_for(lambda: self.active == 0)
            except BaseException:
                self.changing = False
                self.condition.notify_all()
                raise
        try:
            yield
        finally:
            async with self.condition:
                self.changing = False
                self.condition.notify_all()


async def run_blocking(function, *args):
    """Keep an operation leased until its worker finishes, even on cancellation."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise
