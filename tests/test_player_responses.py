"""Regression coverage using BluOS's documented XML response formats."""
import unittest
import httpx
from app.bluos import BluOSPlayer


class VolumeResponses(unittest.IsolatedAsyncioTestCase):
    async def read_volume(self, xml):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=xml))) as client:
            return await BluOSPlayer('player.test', client=client).volume()

    async def test_documented_root_text_volume(self):
        volume = await self.read_volume('<volume db="-49.9" mute="0" offsetDb="0">15</volume>')
        self.assertEqual(volume.level, 15)
        self.assertEqual(volume.db, -49.9)
        self.assertFalse(volume.mute)
        self.assertFalse(volume.is_fixed)

    async def test_muted_and_fixed_outputs(self):
        muted = await self.read_volume('<volume db="-100" mute="1" muteVolume="11">0</volume>')
        self.assertEqual(muted.level, 0)
        self.assertTrue(muted.mute)
        fixed = await self.read_volume('<volume mute="0">-1</volume>')
        self.assertTrue(fixed.is_fixed)

    async def test_nested_response_remains_supported(self):
        volume = await self.read_volume('<volume><volume>28</volume><db>-20</db><mute>1</mute></volume>')
        self.assertEqual(volume.level, 28)
        self.assertEqual(volume.db, -20)
        self.assertTrue(volume.mute)


if __name__ == '__main__':
    unittest.main()
