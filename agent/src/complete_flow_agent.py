"""
===================================================================================
LIVEKIT AGENT WITH FULL ROOM TRANSCRIPTION + CALL TRANSFER
PRODUCTION READY - ALL CONVERSATIONS TRANSCRIBED
===================================================================================
"""

import logging
import os
import time
import asyncio
from pathlib import Path
from dotenv import load_dotenv
import aiohttp
from livekit import rtc
from livekit import api
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    cli,
    stt,
)
from livekit.plugins import silero, openai, deepgram

# Load environment variables
current_dir = Path(__file__).parent
env_file = current_dir / ".env"
load_dotenv(dotenv_path=env_file, override=True)

logger = logging.getLogger("agent")
logger.setLevel(logging.INFO)

# ============================================================================
# CCM API HELPER - IMPROVED WITH RETRY LOGIC
# ============================================================================
class CCMClient:
    def __init__(self, url: str):
        self.url = url
        self.session = None
        self.message_queue = asyncio.Queue()
        self.worker_task = None
    
    async def start(self):
        """Start background worker"""
        self.session = aiohttp.ClientSession()
        self.worker_task = asyncio.create_task(self._worker())
        logger.info("[CCM] Background worker started")
    
    async def stop(self):
        """Stop background worker"""
        if self.worker_task:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass
        
        if self.session:
            await self.session.close()
        
        logger.info("[CCM] Background worker stopped")
    
    async def _worker(self):
        """Background worker to process message queue"""
        while True:
            try:
                payload = await self.message_queue.get()
                await self._send_with_retry(payload)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[CCM] Worker error: {e}")
    
    async def _send_with_retry(self, payload: dict, max_retries: int = 3):
        """Send with retry logic"""
        for attempt in range(max_retries):
            try:
                async with self.session.post(
                    f"{self.url}/message/receive",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status in [200, 202]:
                        logger.info(f"[CCM] ✅ Sent: {payload['header']['sender']['type']}")
                        return True
                    else:
                        error = await resp.text()
                        logger.error(f"[CCM] ❌ {resp.status}: {error}")
            except Exception as e:
                logger.error(f"[CCM] Attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
        
        logger.error(f"[CCM] ❌ Failed after {max_retries} attempts")
        return False
    
    def send(self, call_id: str, customer_id: str, message: str, sender_type: str):
        """Queue message for sending (non-blocking)"""
        payload = {
            "id": call_id,
            "header": {
                "channelData": {
                    "channelCustomerIdentifier": customer_id,
                    "serviceIdentifier": "682200",
                    "channelTypeCode": "CX_VOICE"
                },
                "sender": {
                    "id": "6540b0fc90b3913194d45525" if sender_type == "BOT" else 
                          "460df46c-adf9-11ed-afa1-0242ac120002" if sender_type == "CONNECTOR" else
                          "agent_live_transfer",
                    "type": sender_type,
                    "senderName": "Voice Bot" if sender_type == "BOT" else 
                                 "WEB_CONNECTOR" if sender_type == "CONNECTOR" else "Live Agent",
                    "additionalDetail": None
                },
                "language": {},
                "timestamp": str(int(time.time() * 1000)),
                "securityInfo": {},
                "stamps": [],
                "intent": "",
                "originalMessageId": None,
                "schedulingMetaData": None,
                "entities": {}
            },
            "body": {
                "type": "PLAIN",
                "markdownText": message
            }
        }
        
        self.message_queue.put_nowait(payload)
        logger.info(f"[CCM] Queued: {sender_type} - {message[:50]}...")

# Global CCM client
ccm_client = CCMClient("https://efcx4-voice.expertflow.com/ccm")

# ============================================================================
# AGENT DEFINITION
# ============================================================================
class Assistant(Agent):
    def __init__(self, call_id: str, customer_id: str) -> None:
        super().__init__(
            instructions="""You are a helpful voice AI assistant for Expertflow Support.

IMPORTANT: Your very first response when the call starts must be exactly: "Welcome to Expertflow Support, let me know how I can help you?"

When a customer asks to speak with a human agent or mentions "transfer", "agent", 
"representative", "human", "connect me", say "Let me connect you with our team" then STOP speaking.""",
        )
        self.call_id = call_id
        self.customer_id = customer_id

# ============================================================================
# SERVER SETUP
# ============================================================================
server = AgentServer()

def prewarm(proc: JobProcess):
    """Preload VAD model"""
    proc.userdata["vad"] = silero.VAD.load()

server.setup_fnc = prewarm

# ============================================================================
# MAIN AGENT HANDLER
# ============================================================================
@server.rtc_session(agent_name="")
async def my_agent(ctx: JobContext):
    # Start CCM client
    await ccm_client.start()
    
    ctx.log_context_fields = {"room": ctx.room.name}
    
    call_id = ctx.room.name
    customer_id = ctx.room.metadata if ctx.room.metadata else "unknown"
    
    logger.info(f"[CALL] 🔵 NEW: Room={call_id}, Customer={customer_id}")
    
    # State tracking
    transfer_triggered = {"value": False}
    session_ref = {"session": None}
    ai_active = {"value": True}
    transcription_started = {"value": False}
    
    # ========================================================================
    # ROOM-LEVEL TRANSCRIPTION HANDLER (Works even after AI leaves!)
    # ========================================================================
    async def start_room_transcription():
        """Start continuous room transcription using Deepgram"""
        if transcription_started["value"]:
            return
        
        transcription_started["value"] = True
        logger.info("[TRANSCRIPTION] Starting room-level transcription")
        
        try:
            # Get all audio tracks in room
            for participant in ctx.room.remote_participants.values():
                participant_identity = participant.identity
                is_customer = not participant_identity.startswith("human-agent")
                
                logger.info(f"[TRANSCRIPTION] Subscribing to: {participant_identity}")
                
                for track_pub in participant.track_publications.values():
                    if track_pub.track and track_pub.track.kind == rtc.TrackKind.KIND_AUDIO:
                        asyncio.create_task(
                            transcribe_track(track_pub.track, participant_identity, is_customer)
                        )
        except Exception as e:
            logger.error(f"[TRANSCRIPTION] Error: {e}")
    
    async def transcribe_track(track: rtc.Track, participant_identity: str, is_customer: bool):
        """Transcribe a specific audio track"""
        logger.info(f"[TRANSCRIPTION] Starting for {participant_identity}")
        
        try:
            # Create STT stream using Deepgram
            stt = deepgram.STT(
                model="nova-2-general",
                language="en-US",
            )
            
            stream = stt.stream()
            audio_stream = rtc.AudioStream(track)
            
            # Forward audio to STT
            async def forward_audio():
                async for audio_frame in audio_stream:
                    stream.push_frame(audio_frame)
            
            forward_task = asyncio.create_task(forward_audio())
            
            # Process transcriptions
            async for event in stream:
                if event.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                    text = event.alternatives[0].text.strip()
                    if text:
                        sender_type = "CONNECTOR" if is_customer else "AGENT"
                        logger.info(f"[TRANSCRIPTION] {sender_type}: {text}")
                        ccm_client.send(call_id, customer_id, text, sender_type)
            
            forward_task.cancel()
            
        except Exception as e:
            logger.error(f"[TRANSCRIPTION] Error for {participant_identity}: {e}")
    
    # ========================================================================
    # TRANSFER FUNCTION
    # ========================================================================
    async def execute_transfer():
        """Transfer to human agent"""
        if transfer_triggered["value"]:
            return
            
        transfer_triggered["value"] = True
        logger.info(f"[TRANSFER] 🔴 EXECUTING")
        
        ccm_client.send(call_id, customer_id, "Connecting you to our live agent...", "BOT")
        
        try:
            livekit_api = api.LiveKitAPI(
                url=os.getenv("LIVEKIT_URL"),
                api_key=os.getenv("LIVEKIT_API_KEY"),
                api_secret=os.getenv("LIVEKIT_API_SECRET")
            )
            
            result = await livekit_api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=call_id,
                    sip_trunk_id="ST_W7jqvDFA2VgG",
                    sip_call_to="99900",
                    participant_identity=f"human-agent-{customer_id}",
                    participant_name="Human Agent",
                    participant_metadata='{"reason": "customer_request"}',
                )
            )
            
            logger.info(f"[TRANSFER] ✅ Success: {result.sip_call_id}")
            ccm_client.send(call_id, customer_id, "Transfer initiated", "BOT")
            
        except Exception as e:
            logger.error(f"[TRANSFER] ❌ Failed: {e}", exc_info=True)
            transfer_triggered["value"] = False
    
    # ========================================================================
    # ROOM EVENTS
    # ========================================================================
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        logger.info(f"[ROOM] 👤 Joined: {participant.identity}")
        
        # Start transcription when participant joins
        asyncio.create_task(start_room_transcription())
        
        # Human agent joined - AI leaves
        if participant.identity.startswith("human-agent"):
            logger.info(f"[ROOM] 🟢 Human agent connected")
            
            async def ai_leave():
                await asyncio.sleep(1)
                ai_active["value"] = False
                logger.info("[AGENT] 🤖 AI leaving...")
                
                if session_ref["session"]:
                    await session_ref["session"].aclose()
                
                await ctx.disconnect()
                logger.info("[AGENT] ✅ AI left - Transcription continues")
            
            asyncio.create_task(ai_leave())
    
    @ctx.room.on("track_subscribed")
    def on_track_subscribed(track: rtc.Track, publication: rtc.TrackPublication, participant: rtc.RemoteParticipant):
        logger.info(f"[ROOM] 🎧 Track: {participant.identity} - {track.kind}")
        
        # Start transcription when track is ready
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            asyncio.create_task(start_room_transcription())
    
    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        logger.info(f"[ROOM] 👋 Left: {participant.identity}")
    
    # ========================================================================
    # SESSION
    # ========================================================================
    session = AgentSession(
        llm=openai.realtime.RealtimeModel(
            model="gpt-4o-realtime-preview-2024-12-17",
            voice="alloy",
            temperature=0.8,
            modalities=['text', 'audio'],
            turn_detection={
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 500,
            },
        ),
        vad=ctx.proc.userdata["vad"],
    )
    
    session_ref["session"] = session
    
    # ========================================================================
    # USER TRANSCRIPT (Customer speaks - BACKUP, room transcription is primary)
    # ========================================================================
    @session.on("user_input_transcribed")
    def on_user_input_transcribed(event):
        if not event.is_final or not ai_active["value"]:
            return
        
        transcript = event.transcript
        logger.info(f"[USER-AI] 👤 {transcript}")
        
        # Check transfer keywords
        keywords = ["transfer", "human", "agent", "representative", "person", "someone", "connect"]
        if any(k in transcript.lower() for k in keywords):
            logger.info(f"[TRANSFER] Keyword detected")
            asyncio.create_task(execute_transfer())
    
    # ========================================================================
    # AGENT RESPONSE (AI speaks - BACKUP, room transcription is primary)
    # ========================================================================
    @session.on("conversation_item_added")
    def on_conversation_item_added(event):
        if not ai_active["value"]:
            return
        
        item = event.item
        if item.role == "assistant" and hasattr(item, 'text_content') and item.text_content:
            logger.info(f"[AGENT-AI] 🤖 {item.text_content}")
    
    # ========================================================================
    # START
    # ========================================================================
    await session.start(
        agent=Assistant(call_id, customer_id),
        room=ctx.room,
    )
    
    await ctx.connect()
    
    logger.info(f"[AGENT] ✅ Connected")
    
    # Send welcome greeting
    welcome_msg = "Welcome to Expertflow Support, let me know how I can help you?"
    await session.say(welcome_msg, allow_interruptions=True)
    ccm_client.send(call_id, customer_id, welcome_msg, "BOT")
    
    # Start room transcription immediately
    await start_room_transcription()

# ============================================================================
# CLEANUP
# ============================================================================
# @server.on("shutdown")
# async def on_shutdown():
#     await ccm_client.stop()

# ============================================================================
# RUN
# ============================================================================
if __name__ == "__main__":
    cli.run_app(server)
