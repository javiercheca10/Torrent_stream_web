import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bencodepy
from fastapi import HTTPException
from app import main as m


class PolicyTests(unittest.TestCase):
    def test_valid_torrent(self):
        data = bencodepy.encode({b'info': {b'name': b'x.mp4', b'length': 16384,
                                         b'piece length': 16384, b'pieces': b'a' * 20}})
        self.assertEqual(len(m.validate_torrent(data)), 40)

    def test_invalid_metadata(self):
        for data in [b'', b'bad', b'd4:infodee', bencodepy.encode({b'info': {b'name': b'x',
                     b'length': -1, b'piece length': 16384, b'pieces': b'a'*20}})]:
            with self.assertRaises(HTTPException):
                m.validate_torrent(data)

    def test_grace_all_connections(self):
        s = dict(created=1000, last_activity=1000, started=True, tasks={'active'}, bg=set())
        self.assertIsNone(m.expiry(s, 1000 + m.GRACE + 1))
        s['tasks'].clear()
        self.assertEqual(m.expiry(s, 1000 + m.GRACE), 'disconnect_grace')

    def test_bg_tasks_prevent_grace(self):
        s = dict(created=1000, last_activity=1000, started=True, tasks=set(), bg={'bg_task'})
        self.assertIsNone(m.expiry(s, 1000 + m.GRACE + 1))

    def test_never_played_ttl(self):
        s = dict(created=1000, last_activity=1000, started=False, tasks=set(), bg=set())
        self.assertIsNone(m.expiry(s, 1000 + m.GRACE + 1))
        self.assertEqual(m.expiry(s, 1000 + m.NEVER_PLAYED_TTL), 'never_played')
        self.assertEqual(m.expiry(s, 1000 + m.TTL), 'ttl')

    def test_ttl_even_with_connection(self):
        s = dict(created=1000, last_activity=1000, started=True, tasks={'active'}, bg=set())
        self.assertEqual(m.expiry(s, 1000 + m.TTL), 'ttl')

    def test_paused_ttl(self):
        s = dict(created=1000, last_activity=1000, started=True, tasks=set(), bg=set(),
                 paused=True, paused_at=1000)
        self.assertIsNone(m.expiry(s, 1000 + m.PAUSED_TTL - 1))
        self.assertEqual(m.expiry(s, 1000 + m.PAUSED_TTL), 'paused_ttl')

    def test_paused_ttl_bypassed_by_ttl(self):
        s = dict(created=1000, last_activity=1000, started=True, tasks=set(), bg=set(),
                 paused=True, paused_at=1000)
        self.assertEqual(m.expiry(s, 1000 + m.TTL), 'ttl')

    def test_unpaused_no_paused_ttl(self):
        s = dict(created=1000, last_activity=1000, started=True, tasks=set(), bg=set(),
                 paused=False, paused_at=None)
        self.assertEqual(m.expiry(s, 1000 + m.PAUSED_TTL + 1), 'disconnect_grace')


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_cache_removal_with_active_other_connection(self):
        s = dict(id='test', hash='a'*40, tasks={'other'}, bg=set(), state='deleting')
        with patch.object(m, 'remove_hash', new_callable=AsyncMock) as remove:
            await m.reap(s)
            remove.assert_not_awaited()

    async def test_no_cache_removal_with_bg_task(self):
        s = dict(id='test', hash='a'*40, tasks=set(), bg={'bg_task'}, state='deleting')
        with patch.object(m, 'remove_hash', new_callable=AsyncMock) as remove:
            await m.reap(s)
            remove.assert_not_awaited()

    async def test_failed_cleanup_retains_ownership(self):
        s = dict(id='test', hash='a'*40, tasks=set(), bg=set(), state='deleting')
        m.sessions['test'] = s
        try:
            with patch.object(m, 'remove_hash', new_callable=AsyncMock, side_effect=OSError('test failure')):
                await m.reap(s)
            self.assertIn('test', m.sessions)
        finally:
            m.sessions.pop('test')


class AccessTests(unittest.TestCase):
    def test_owner_is_required(self):
        session = {"owner_chat_id": 123}
        m.require_owner(session, SimpleNamespace(headers={"X-Owner-Chat-Id": "123"}))
        with self.assertRaises(HTTPException) as context:
            m.require_owner(session, SimpleNamespace(headers={"X-Owner-Chat-Id": "456"}))
        self.assertEqual(context.exception.status_code, 403)

    def test_public_status_hides_session_capabilities(self):
        session = dict(id="secret-id", hash="a" * 40, owner_chat_id=123, play_token="secret-token",
                       created=1000, last_activity=1000, started=False, state="ready", files=[], selected=0,
                       tasks=set(), bg=set(), paused=False, paused_at=None, buffered_bytes=10,
                       prefetch_bytes=0, buffer_target=20, title="Film")
        result = m.status(session)
        self.assertNotIn("id", result)
        self.assertNotIn("play_token", result)
        self.assertNotIn("owner_chat_id", result)


if __name__ == '__main__':
    unittest.main()
