import asyncio
import socket
from contextlib import asynccontextmanager
from time import perf_counter
from typing import AsyncIterator
from uuid import uuid4

import httpx
import uvicorn

from mock_checker import app
from src.checker.client import call_checker


@asynccontextmanager
async def mock_url() -> AsyncIterator[str]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            else:
                raise RuntimeError("Checker mock did not start")
            yield f"http://127.0.0.1:{port}/check"
        finally:
            server.should_exit = True
            await task


async def measure(url: str, delay: float, limit: int, timeout: float) -> None:
    slots = asyncio.Semaphore(limit)
    active = peak = successes = timeouts = 0

    async with httpx.AsyncClient(timeout=timeout) as client:

        async def one_call() -> None:
            nonlocal active, peak, successes, timeouts
            async with slots:
                active += 1
                peak = max(peak, active)
                try:
                    async with asyncio.timeout(timeout):
                        await call_checker(
                            client,
                            f"{url}?delay_seconds={delay}",
                            uuid4(),
                            "https://example.com/student/repository",
                        )
                    successes += 1
                except httpx.TimeoutException, TimeoutError:
                    timeouts += 1
                finally:
                    active -= 1

        started = perf_counter()
        await asyncio.gather(*(one_call() for _ in range(10)))
        elapsed = perf_counter() - started

    print(
        f"{delay:.2f},{limit},{timeout:.2f},{elapsed:.3f},{peak},{successes},{timeouts}"
    )


async def main() -> None:
    print("delay_s,limit,timeout_s,elapsed_s,peak,successes,timeouts")
    async with mock_url() as url:
        for delay in (0.15, 0.30):
            for limit in (1, 5):
                await measure(url, delay, limit, timeout=2.0)
        await measure(url, delay=0.30, limit=5, timeout=0.05)


if __name__ == "__main__":
    asyncio.run(main())
