"""
============================================================================
LIVEKIT AGENT WITH OPENAI REALTIME API + CALL TRANSFER TO HUMAN AGENT
FULL TRANSCRIPTION TO CCM - PRODUCTION READY
============================================================================
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
from livekit.plugins import silero
from livekit.plugins import openai
from livekit.plugins import deepgram

# Load environment variables
current_dir = Path(__file__).parent
env_file = current_dir / ".env"
load_dotenv(dotenv_path=env_file, override=True)

logger = logging.getLogger("agent")
logger.setLevel(logging.INFO)

# ============================================================================
# CCM API HELPER - WITH QUEUE FOR RELIABILITY
# ============================================================================
class CCMClient:
    def __init__(self, url: str):
        self.url = url
        self.queue = asyncio.Queue()
        self.worker_task = None
        
    async def start(self):
        """Start background worker"""
        self.worker_task = asyncio.create_task(self._worker())
        logger.info("[CCM] Worker started")
    
    async def _worker(self):
        """Process messages in background"""
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    payload = await self.queue.get()
                    await self._send(session, payload)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"[CCM] Worker error: {e}")
    
    async def _send(self, session, payload):
        """Send to CCM with retry"""
        for attempt in range(3):
            try:
                async with session.post(
                    f"{self.url}/message/receive",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status in [200, 202]:
                        logger.info(f"[CCM] ✅ {payload['header']['sender']['type']}")
                        return
                    else:
                        logger.error(f"[CCM] Status {resp.status}")
            except Exception as e:
                logger.error(f"[CCM] Attempt {attempt + 1}: {e}")
                if attempt < 2:
                    await asyncio.sleep(1)
    
    def send(self, call_id: str, customer_id: str, message: str, sender_type: str):
        """Queue message for sending"""
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
                          "460df46c-adf9-11ed-afa1-0242ac120002",
                    "type": sender_type,
                    "senderName": "Voice Bot" if sender_type == "BOT" else 
                                 "Human Agent" if sender_type == "AGENT" else "WEB_CONNECTOR",
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
        self.queue.put_nowait(payload)
        logger.info(f"[CCM] Queued {sender_type}: {message[:50]}")

ccm_client = CCMClient("https://efcx4-voice.expertflow.com/ccm")

# ============================================================================
# AGENT DEFINITION
# ============================================================================
class Assistant(Agent):
    def __init__(self, call_id: str, customer_id: str) -> None:
        super().__init__(
            instructions="""You are a helpful voice AI assistant.

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
    await ccm_client.start()
    
    ctx.log_context_fields = {"room": ctx.room.name}
    
    call_id = ctx.room.name
    customer_id = ctx.room.metadata if ctx.room.metadata else "unknown"
    
    logger.info(f"🔵 NEW CALL: Room={call_id}, Customer={customer_id}")
    
    transfer_triggered = {"value": False}
    session_ref = {"session": None}
    ai_active = {"value": True}
    greeting_played = {"value": False}
    
    # ========================================================================
    # DEEPGRAM TRANSCRIPTION FOR CUSTOMER (works after AI leaves)
    # ========================================================================
    async def start_customer_transcription():
        """Transcribe customer audio using Deepgram"""
        logger.info("[DEEPGRAM] Starting customer transcription")
        
        # Find customer participant
        customer_participant = None
        for p in ctx.room.remote_participants.values():
            if not p.identity.startswith("human-agent"):
                customer_participant = p
                break
        
        if not customer_participant:
            logger.warning("[DEEPGRAM] No customer found")
            return
        
        # Find audio track
        audio_track = None
        for pub in customer_participant.track_publications.values():
            if pub.track and pub.track.kind == rtc.TrackKind.KIND_AUDIO:
                audio_track = pub.track
                break
        
        if not audio_track:
            logger.warning("[DEEPGRAM] No audio track")
            return
        
        logger.info(f"[DEEPGRAM] Transcribing {customer_participant.identity}")
        
        try:
            # Create Deepgram STT
            deepgram_stt = deepgram.STT(
                model="nova-2-general",
                language="en-US",
            )
            
            audio_stream = rtc.AudioStream(audio_track)
            stt_stream = deepgram_stt.stream()
            
            # Forward audio to Deepgram
            async def forward():
                async for frame in audio_stream:
                    stt_stream.push_frame(frame)
            
            forward_task = asyncio.create_task(forward())
            
            # Process transcripts
            async for event in stt_stream:
                if event.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                    text = event.alternatives[0].text.strip()
                    if text:
                        logger.info(f"[CUSTOMER] {text}")
                        ccm_client.send(call_id, customer_id, text, "CONNECTOR")
            
            forward_task.cancel()
            
        except Exception as e:
            logger.error(f"[DEEPGRAM] Error: {e}", exc_info=True)
    
    # ========================================================================
    # TRANSFER FUNCTION
    # ========================================================================
    async def execute_transfer():
        """Execute SIP transfer to human agent"""
        if transfer_triggered["value"]:
            logger.info("⏭️ Transfer already in progress")
            return
            
        transfer_triggered["value"] = True
        logger.info(f"🔴 EXECUTING TRANSFER")
        
        ccm_client.send(call_id, customer_id, "Connecting you to our live agent...", "BOT")
        
        try:
            livekit_api = api.LiveKitAPI(
                url=os.getenv("LIVEKIT_URL"),
                api_key=os.getenv("LIVEKIT_API_KEY"),
                api_secret=os.getenv("LIVEKIT_API_SECRET")
            )
            
            outbound_trunk_id = "ST_W7jqvDFA2VgG"
            agent_extension = "99900"
            
            logger.info(f"📞 Calling: {agent_extension}")
            
            transfer_result = await livekit_api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=call_id,
                    sip_trunk_id=outbound_trunk_id,
                    sip_call_to=f"{agent_extension}",
                    participant_identity=f"human-agent-general",
                    participant_name=f"Human Agent",
                    participant_metadata='{"reason": "customer_request"}',
                )
            )
            
            logger.info(f"✅ TRANSFER SUCCESS: {transfer_result.sip_call_id}")
            ccm_client.send(call_id, customer_id, "Transfer initiated", "BOT")
            
        except Exception as e:
            logger.error(f"❌ TRANSFER FAILED: {e}", exc_info=True)
            transfer_triggered["value"] = False
            ccm_client.send(call_id, customer_id, "Transfer failed. Please try again.", "BOT")
    
    # ========================================================================
    # ROOM EVENTS
    # ========================================================================
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        logger.info(f"👤 JOINED: {participant.identity}")
        
        if participant.identity.startswith("human-agent"):
            logger.info(f"🟢 HUMAN AGENT CONNECTED - AI WILL LEAVE")
            
            async def ai_leave():
                await asyncio.sleep(1)
                ai_active["value"] = False
                logger.info("🤖 AI leaving room...")
                
                if session_ref["session"]:
                    await session_ref["session"].aclose()
                
                # Start Deepgram transcription for customer after AI leaves
                asyncio.create_task(start_customer_transcription())
                
                logger.info("✅ AI left - Customer transcription continues")
            
            asyncio.create_task(ai_leave())
    
    class Assistant(Agent):
     def __init__(self, call_id: str, customer_id: str):
        super().__init__(instructions="...")
        self.call_id = call_id
        self.customer_id = customer_id
        self.greeted = False

    async def on_enter(self):
        if self.greeted:
            return
        self.greeted = True

        welcome = "Welcome to Expertflow Support, let me know how I can help you?"
        logger.info("🎙️ Greeting")

        ccm_client.send(self.call_id, self.customer_id, welcome, "BOT")

        self.session.generate_reply(
            instructions=f'Say exactly: "{welcome}"'
        )

        logger.info(f"🎧 TRACK: {participant.identity} - {track.kind}")
        
        # Play greeting when first audio track is ready
        if track.kind == rtc.TrackKind.KIND_AUDIO and not greeting_played["value"]:
            greeting_played["value"] = True
            
            async def play_greeting():
                await asyncio.sleep(1)  # Wait for session to be ready
                welcome = "Welcome to Expertflow Support, let me know how I can help you?"
                logger.info(f"🎙️ Playing greeting")
                
                ccm_client.send(call_id, customer_id, welcome, "BOT")
                
                if session_ref["session"]:
                    await session_ref["session"].say(welcome, allow_interruptions=True)
            
            asyncio.create_task(play_greeting())
    
    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        logger.info(f"👋 LEFT: {participant.identity}")
    
    # ========================================================================
    # OPENAI REALTIME SESSION
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
    # USER INPUT TRANSCRIBED (Customer speaks to AI)
    # ========================================================================
    @session.on("user_input_transcribed")
    def on_user_input_transcribed(event):
        if not event.is_final or not ai_active["value"]:
            return
        
        transcript = event.transcript
        logger.info(f"👤 USER: {transcript}")
        
        # Send to CCM
        ccm_client.send(call_id, customer_id, transcript, "CONNECTOR")
        
        # Check for transfer keywords
        transfer_keywords = ["transfer", "human", "agent", "representative", "person", "someone"]
        if any(keyword in transcript.lower() for keyword in transfer_keywords):
            logger.info(f"🔍 TRANSFER KEYWORD DETECTED")
            asyncio.create_task(execute_transfer())
    
    # ========================================================================
    # CONVERSATION ITEM ADDED (AI speaks)
    # ========================================================================
    @session.on("conversation_item_added")
    def on_conversation_item_added(event):
        if not ai_active["value"]:
            return
        
        item = event.item
        
        if item.role == "assistant" and hasattr(item, 'text_content') and item.text_content:
            logger.info(f"🤖 AGENT: {item.text_content}")
            ccm_client.send(call_id, customer_id, item.text_content, "BOT")
    
    # Start session
    await session.start(
        agent=Assistant(call_id, customer_id),
        room=ctx.room,
    )
    
    await ctx.connect()
    
    logger.info(f"✅ AGENT CONNECTED TO ROOM: {call_id}")

# ============================================================================
# RUN SERVER
# ============================================================================
if __name__ == "__main__":
    cli.run_app(server)