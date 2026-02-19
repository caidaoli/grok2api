import asyncio

from app.core import storage as storage_mod


def test_local_storage_nested_lock_names_should_not_deadlock():
    if storage_mod.fcntl is None:
        return

    async def _run() -> bool:
        storage = storage_mod.LocalStorage()

        async def _nested() -> bool:
            async with storage.acquire_lock("token_select", timeout=1):
                async with storage.acquire_lock("tokens_save", timeout=1):
                    return True

        return await asyncio.wait_for(_nested(), timeout=0.5)

    assert asyncio.run(_run()) is True
