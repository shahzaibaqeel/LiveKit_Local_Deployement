# ===========================================================================
# FILE: custom_gtts.py - FIXED VERSION FOR LIVEKIT 1.3.10
# ===========================================================================

import asyncio
import logging
import io
import numpy as np
import os
from gtts import gTTS
from pydub import AudioSegment
from livekit import rtc
from livekit.agents import tts, utils, APIConnectOptions

logger = logging.getLogger("custom-gtts")

# ============================================================================ 
# Set FFmpeg path globally
# ============================================================================
FFMPEG_PATH = r"C:\ffmpeg\bin\ffmpeg.exe"
FFPROBE_PATH = r"C:\ffmpeg\bin\ffprobe.exe"
if os.path.exists(FFMPEG_PATH) and os.path.exists(FFPROBE_PATH):
    AudioSegment.converter = FFMPEG_PATH
    AudioSegment.ffprobe = FFPROBE_PATH
    os.environ["PATH"] = r"C:\ffmpeg\bin" + os.pathsep + os.environ.get("PATH", "")
    logger.info(f"✓ FFmpeg configured: {FFMPEG_PATH}")
else:
    raise RuntimeError(f"FFmpeg not found at {FFMPEG_PATH}")

# ============================================================================ 
class CustomGTTS(tts.TTS):
    """Google Text-to-Speech (gTTS) implementation for LiveKit"""
    def __init__(self, language: str = "en", tld: str = "com"):
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=24000,
            num_channels=1,
        )
        self._language = language
        self._tld = tld
        logger.info(f"✓ gTTS initialized (language={language}, tld={tld})")

    def synthesize(self, text: str, *, conn_options: APIConnectOptions | None = None) -> "GTTSStream":
        logger.info(f"Synthesize: '{text[:50]}...'")
        return GTTSStream(
            tts=self,
            text=text,
            language=self._language,
            tld=self._tld,
            sample_rate=self._sample_rate,
            conn_options=conn_options or APIConnectOptions()
        )

# ============================================================================ 
class GTTSStream(tts.ChunkedStream):
    """gTTS stream - FIXED for proper int16 audio format"""
    def __init__(self, tts: CustomGTTS, text: str, language: str, tld: str, sample_rate: int, conn_options: APIConnectOptions):
        super().__init__(tts=tts, input_text=text, conn_options=conn_options)
        self._text = text
        self._language = language
        self._tld = tld
        self._sample_rate = sample_rate

    async def _run(self, output_emitter: tts.AudioEmitter):
        try:
            logger.info(f"Starting gTTS synthesis: '{self._text[:50]}...'")

            loop = asyncio.get_event_loop()

            # Generate audio in executor
            def _generate_audio():
                AudioSegment.converter = FFMPEG_PATH
                AudioSegment.ffprobe = FFPROBE_PATH
                tts_obj = gTTS(text=self._text, lang=self._language, tld=self._tld, slow=False)
                mp3_buffer = io.BytesIO()
                tts_obj.write_to_fp(mp3_buffer)
                mp3_buffer.seek(0)
                
                # Load and convert to target format
                audio = AudioSegment.from_mp3(mp3_buffer)
                
                # Convert to mono if needed
                if audio.channels > 1:
                    audio = audio.set_channels(1)
                
                # Resample to target sample rate
                audio = audio.set_frame_rate(self._sample_rate)
                
                # Return raw PCM data as bytes (int16 format)
                return audio.raw_data

            pcm_bytes = await loop.run_in_executor(None, _generate_audio)

            # ✅ KEY FIX: Use AudioByteStream to properly chunk the int16 bytes
            # This handles the conversion to AudioFrame objects correctly
            request_id = utils.shortuuid()
            audio_bstream = utils.audio.AudioByteStream(
                sample_rate=self._sample_rate,
                num_channels=1,
            )

            # Initialize the emitter
            output_emitter.initialize(
                request_id=request_id,
                sample_rate=self._sample_rate,
                num_channels=1,
                mime_type="audio/pcm"
            )

            # Push all the int16 bytes through AudioByteStream
            # This automatically creates properly formatted AudioFrame objects
            for frame in audio_bstream.push(pcm_bytes):
                self._event_ch.send_nowait(
                    tts.SynthesizedAudio(
                        request_id=request_id,
                        frame=frame
                    )
                )

            # Flush any remaining audio
            for frame in audio_bstream.flush():
                self._event_ch.send_nowait(
                    tts.SynthesizedAudio(
                        request_id=request_id,
                        frame=frame
                    )
                )

            logger.info(f"✓ Audio sent successfully ({len(pcm_bytes)} bytes)")

        except Exception as e:
            logger.error(f"gTTS failed: {e}", exc_info=True)
            raise
        finally:
            await output_emitter.aclose()