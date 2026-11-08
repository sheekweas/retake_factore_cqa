"""Optional real decoder compatibility smoke test; no network, model or GPU."""
import importlib.util
import io
import unittest
import wave


@unittest.skipUnless(importlib.util.find_spec('faster_whisper'), 'Optional ASR environment not installed')
class AudioDecodeCompatibilityTests(unittest.TestCase):
    def test_installed_decoder_accepts_a_real_wav(self):
        from faster_whisper.audio import decode_audio
        data = io.BytesIO()
        with wave.open(data, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b'\0\0' * 16000)
        data.seek(0)
        decoded = decode_audio(data, sampling_rate=16000)
        self.assertEqual(decoded.ndim, 1)
        self.assertEqual(len(decoded), 16000)
        self.assertTrue((decoded == 0).all())
