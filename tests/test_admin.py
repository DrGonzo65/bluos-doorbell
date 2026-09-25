"""Administration and durable settings tested without real speakers."""
import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app import main
from app.config import Config, RoomPreference, load_config
from app.discovery import PlayerRegistry
from app.orchestrator import DoorbellOrchestrator


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / 'config.yaml'
        self.config_path.write_text('webhook:\n  token: test-token\n')
        self.audio = self.root / 'chimes'
        self.audio.mkdir()
        for path in Path('chimes').glob('*.mp3'):
            shutil.copy(path, self.audio / path.name)
        self.env = patch.dict(os.environ, {'DOORBELL_CONFIG': str(self.config_path),
                                          'DOORBELL_SETTINGS': str(self.root / 'settings.json')})
        self.env.start()
        self.chimes = patch.object(main, 'CHIME_DIR', self.audio)
        self.chimes.start()
        cfg = load_config()
        self.http = httpx.AsyncClient()
        reg = PlayerRegistry(cfg, self.http)
        reg.note('192.0.2.10', 'Kitchen', 'Node', 'test')
        reg.players['192.0.2.10'].mac = 'aabbccddeeff'
        main.state.update(config=cfg, registry=reg, client=self.http,
                          orchestrator=DoorbellOrchestrator(cfg, self.http, reg),
                          bound_address=(cfg.listen_host, cfg.listen_port))
        self.client = TestClient(main.app)
        self.headers = {'X-Doorbell-Token': 'test-token', 'X-Doorbell-Admin': '1'}

    def tearDown(self):
        self.client.close()
        asyncio.run(self.http.aclose())
        self.chimes.stop()
        self.env.stop()
        self.tmp.cleanup()

    def read(self):
        response = self.client.get('/api/settings', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def save(self, snap, **extra):
        return self.client.put('/api/settings', headers=self.headers,
                               json={'settings': snap['settings'], 'revision': snap['revision'], **extra})

    def test_auth_and_cross_origin_write_guard(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/api/settings').status_code, 401)
        self.assertEqual(self.client.post('/api/chimes?filename=x.mp3', headers={
            'X-Doorbell-Token': 'test-token'}, content=b'bad').status_code, 403)
        self.assertNotIn('test-token', json.dumps(self.read()))

    def test_room_opt_in_and_persistence_across_address_and_name_change(self):
        snap = self.read()
        room = snap['rooms'][0]
        self.assertFalse(room['enabled'])
        self.assertEqual(main._orchestrator().target_zones(), [])
        snap['settings']['room_preferences'][room['id']] = {'name': 'Kitchen', 'enabled': True,
                                                         'chime_volume': 22}
        saved = self.save(snap)
        self.assertEqual(saved.status_code, 200, saved.text)
        cfg = load_config()
        self.assertEqual(cfg.zones, [])
        self.assertTrue(cfg.discovery.auto)
        self.assertEqual(self.config_path.read_text(), 'webhook:\n  token: test-token\n')
        reg = PlayerRegistry(cfg, self.http)
        reg.note('192.0.2.55', 'Dining Room', 'Node', 'test')
        reg.players['192.0.2.55'].mac = 'aabbccddeeff'
        self.assertEqual(reg.zones()[0].chime_volume, 22)
        reg.note('192.0.2.56', 'New Room', 'Node', 'test')
        self.assertEqual(len(reg.zones()), 1)
        self.assertEqual(os.stat(self.root / 'settings.json').st_mode & 0o777, 0o600)

    def test_upload_measure_assignment_and_restart(self):
        blob = (self.audio / 'back-door.mp3').read_bytes()
        upload = self.client.post('/api/chimes?filename=my-back-door.mp3', headers=self.headers, content=blob)
        self.assertEqual(upload.status_code, 200, upload.text)
        sound = upload.json()
        self.assertAlmostEqual(sound['duration_seconds'], 2.038, places=2)
        again = self.client.post('/api/chimes?filename=my-back-door.mp3', headers=self.headers, content=blob).json()
        self.assertNotEqual(sound['file'], again['file'])
        snap = self.read()
        snap['settings']['doorbells']['back'] = {'file': sound['file'], 'duration_seconds': 59}
        response = self.save(snap)
        self.assertEqual(response.status_code, 200, response.text)
        cfg = load_config()
        self.assertAlmostEqual(cfg.doorbells['back'].duration_seconds, sound['duration_seconds'])
        self.assertEqual(cfg.chime_for('back').file, sound['file'])
        self.assertEqual(self.client.delete('/api/chimes/'+sound['file'], headers=self.headers).status_code, 409)
        self.assertEqual(self.client.delete('/api/chimes/'+again['file'], headers=self.headers).status_code, 200)

    def test_bad_upload_and_path_and_limits_leave_no_files(self):
        before = set(self.audio.iterdir())
        for filename, blob, expected in [('x.mp3', b'not audio', 422), ('x.html', b'bad', 422),
                                          ('huge.mp3', b'x'*(12*1024*1024+1), 413)]:
            r = self.client.post('/api/chimes', params={'filename': filename}, headers=self.headers, content=blob)
            self.assertEqual(r.status_code, expected, r.text)
        self.assertEqual(set(self.audio.iterdir()), before)
        snap = self.read()
        snap['settings']['chime']['file'] = '../doorbell.mp3'
        self.assertEqual(self.save(snap).status_code, 422)
        self.assertFalse((self.root/'settings.json').exists())

    def test_conflicts_validation_and_active_ring(self):
        snap = self.read()
        snap['settings']['chime']['default_volume'] = 200
        self.assertEqual(self.save(snap).status_code, 422)
        snap['settings']['chime']['default_volume'] = 25
        main._orchestrator()._active = True
        self.assertEqual(self.save(snap).status_code, 409)
        main._orchestrator()._active = False
        self.assertEqual(self.save(snap).status_code, 200)
        self.assertEqual(self.save(snap).status_code, 409)

    def test_token_rotation_and_restart_notice(self):
        snap = self.read()
        snap['settings']['listen_port'] = 8096
        r = self.save(snap, new_token='replacement-token')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['restart_required'])
        self.assertEqual(self.client.get('/api/settings', headers=self.headers).status_code, 401)
        self.headers['X-Doorbell-Token'] = 'replacement-token'
        self.assertEqual(self.read()['settings']['listen_port'], 8096)
        self.assertEqual(load_config().webhook.token, 'replacement-token')

    def test_disk_failure_does_not_apply_changes(self):
        snap = self.read()
        snap['settings']['chime']['default_volume'] = 19
        with patch('app.admin.atomic_save', side_effect=OSError('read only')):
            self.assertEqual(self.save(snap).status_code, 500)
        self.assertEqual(main._config().chime.default_volume, 30)

    def test_name_fallback_preferences_survive_mac_identification(self):
        cfg = main._config()
        cfg.room_preferences['name:kitchen'] = RoomPreference(name='Kitchen', enabled=True)
        self.assertEqual(len(main._orchestrator().target_zones()), 1)

    def test_reused_ip_does_not_inherit_another_speakers_preferences(self):
        reg = main.state['registry']
        cfg = main._config()
        cfg.room_preferences['mac:aabbccddeeff'] = RoomPreference(name='Kitchen', enabled=True)
        async def identify():
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text='<SyncStatus name="New speaker" mac="11:22:33:44:55:66"/>')
            )) as client:
                reg.client = client
                await reg._identify_players()
        asyncio.run(identify())
        self.assertEqual(reg.players['192.0.2.10'].mac, '112233445566')
        self.assertEqual(reg.zones(), [])

    def test_oversized_settings_are_rejected(self):
        response = self.client.put('/api/settings', headers=self.headers, content=b'x' * (128*1024+1))
        self.assertEqual(response.status_code, 413)
        self.assertFalse((self.root / 'settings.json').exists())


if __name__ == '__main__':
    unittest.main()
