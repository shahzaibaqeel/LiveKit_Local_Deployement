import json
import logging
from vosk import Model, KaldiRecognizer
from livekit import rtc
from livekit.agents import stt, utils, APIConnectOptions
import numpy as np

logger = logging.getLogger("custom-vosk-stt")

class VoskSTT(stt.STT):
    """Custom Vosk STT implementation for LiveKit"""
    
    def __init__(self, model_path: str, sample_rate: int = 16000):
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True,
                interim_results=True
            )
        )
        self.model_path = model_path
        self.sample_rate = sample_rate
        self._model = None
        logger.info(f"Vosk STT initialized with model path: {model_path}")
        
    def _ensure_model_loaded(self):
        """Lazy load the Vosk model"""
        if self._model is None:
            logger.info(f"Loading Vosk model from {self.model_path}")
            self._model = Model(self.model_path)
            logger.info("Vosk model loaded successfully")
    
    async def _recognize_impl(
        self, 
        buffer: utils.AudioBuffer, 
        *, 
        language: str | None = None,
        conn_options: APIConnectOptions | None = None
    ) -> stt.SpeechEvent:
        """Non-streaming recognition - required by STT base class"""
        self._ensure_model_loaded()
        recognizer = KaldiRecognizer(self._model, self.sample_rate)
        
        # Convert audio buffer to bytes
        audio_data = np.frombuffer(buffer.data, dtype=np.int16)
        audio_bytes = audio_data.tobytes()
        
        # Process audio
        if recognizer.AcceptWaveform(audio_bytes):
            result = json.loads(recognizer.Result())
        else:
            result = json.loads(recognizer.PartialResult())
        
        text = result.get("text", "")
        
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[
                stt.SpeechData(
                    text=text,
                    language=language or "en-US"
                )
            ]
        )
    
    def stream(
        self,
        *,
        language: str | None = None,
        conn_options: APIConnectOptions | None = None,
    ) -> "VoskSpeechStream":
        """Create a streaming STT session"""
        self._ensure_model_loaded()
        return VoskSpeechStream(
            stt=self,
            model=self._model,
            sample_rate=self.sample_rate,
            language=language or "en-US",
            conn_options=conn_options or APIConnectOptions()
        )


class VoskSpeechStream(stt.SpeechStream):
    """Streaming STT implementation for Vosk"""
    
    def __init__(
        self, 
        stt: VoskSTT,
        model: Model, 
        sample_rate: int, 
        language: str,
        conn_options: APIConnectOptions
    ):
        super().__init__(stt=stt, conn_options=conn_options, sample_rate=sample_rate)
        self._model = model
        self._language = language
        self._recognizer = KaldiRecognizer(model, sample_rate)
        self._recognizer.SetWords(True)
        
    async def _run(self):
        """Process incoming audio chunks"""
        try:
            async for frame in self._input_ch:
                if isinstance(frame, rtc.AudioFrame):
                    # Convert frame to bytes (16-bit PCM)
                    audio_data = np.frombuffer(frame.data, dtype=np.int16)
                    audio_bytes = audio_data.tobytes()
                    
                    # Process with Vosk
                    if self._recognizer.AcceptWaveform(audio_bytes):
                        # Final result
                        result = json.loads(self._recognizer.Result())
                        text = result.get("text", "")
                        
                        if text:
                            event = stt.SpeechEvent(
                                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                                alternatives=[
                                    stt.SpeechData(
                                        text=text,
                                        language=self._language
                                    )
                                ]
                            )
                            self._event_ch.send_nowait(event)
                    else:
                        # Partial result
                        partial = json.loads(self._recognizer.PartialResult())
                        partial_text = partial.get("partial", "")
                        
                        if partial_text:
                            event = stt.SpeechEvent(
                                type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                                alternatives=[
                                    stt.SpeechData(
                                        text=partial_text,
                                        language=self._language
                                    )
                                ]
                            )
                            self._event_ch.send_nowait(event)
        except Exception as e:
            logger.error(f"Error in Vosk STT stream: {e}", exc_info=True)
        finally:
            # Get final result when stream ends
            final = json.loads(self._recognizer.FinalResult())
            final_text = final.get("text", "")
            if final_text:
                event = stt.SpeechEvent(
                    type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                    alternatives=[
                        stt.SpeechData(
                            text=final_text,
                            language=self._language
                        )
                    ]
                )
                self._event_ch.send_nowait(event)
            
            await self._event_ch.aclose()
