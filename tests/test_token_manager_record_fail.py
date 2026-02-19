import asyncio

import pytest

from app.core.logger import logger
from app.services.token.manager import TokenManager
from app.services.token.models import FAIL_THRESHOLD, TokenInfo, TokenStatus
from app.services.token.pool import TokenPool


@pytest.mark.parametrize("status_code", [401, 403])
def test_auth_fail_marks_token_unavailable_and_warning_logs_full_token(monkeypatch, status_code):
    async def _run():
        token_value = "tok-full-value-for-warning-check"
        token_info = TokenInfo(token=token_value, quota=10, status=TokenStatus.ACTIVE, fail_count=0)

        pool = TokenPool("ssoBasic")
        pool.add(token_info)

        mgr = TokenManager()
        mgr.pools = {"ssoBasic": pool}

        monkeypatch.setattr(mgr, "_schedule_save", lambda: None)

        warning_messages = []
        sink_id = logger.add(lambda message: warning_messages.append(message.record["message"]), level="WARNING")
        try:
            ok = await mgr.record_fail(token_value, status_code, "auth-failed")
        finally:
            logger.remove(sink_id)

        assert ok is True
        assert token_info.status == TokenStatus.EXPIRED
        assert token_info.fail_count == FAIL_THRESHOLD
        assert any(token_value in msg for msg in warning_messages)

    asyncio.run(_run())
