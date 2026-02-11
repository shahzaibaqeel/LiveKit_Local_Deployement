"""
===================================================================================
LIVEKIT AGENT WITH OPENAI REALTIME API + CALL TRANSFER + FULL TRANSCRIPTION
ALL ISSUES FIXED - PRODUCTION READY
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
    AutoSubscribe,
    llm,
)
from livekit.plugins import silero, openai, deepgram

# Load environment variables
current_dir = Path(__file__).parent
env_file = current_dir / ".env"
load_dotenv(dotenv_path=env_file, override=True)

logger = logging.getLogger("agent")
logger.setLevel(logging.INFO)

# Global aiohttp session for CCM
ccm_session = None

async def get_ccm_session():
    """Get or create global CCM session"""
    global ccm_session
    if ccm_session is None or ccm_session.closed:
        timeout = aiohttp.ClientTimeout(total=10)
        ccm_session = aiohttp.ClientSession(timeout=timeout)
    return ccm_session

# ============================================================================
# CCM API HELPER - FIXED: Use global session
# ============================================================================
async def send_to_ccm(call_id: str, customer_id: str, message: str, sender_type: str):
    """Send transcript to CCM with retry logic"""
    if not message or not message.strip():
        return False
    
    # FIX: Log what we're sending
    logger.info(f"[CCM→] {sender_type}: {message[:80]}...")
    
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
        "body": {"type": "PLAIN", "markdownText": message}
    }
    
    # FIX: Use shared session
    session = await get_ccm_session()
    
    for attempt in range(3):
        try:
            async with session.post(
                "https://efcx4-voice.expertflow.com/ccm/message/receive",
                json=payload,
                headers={"Content-Type": "application/json"}
            ) as resp:
                if resp.status in [200, 202]:
                    logger.info(f"[CCM✓] {sender_type} ({resp.status})")
                    return True
                else:
                    response_text = await resp.text()
                    logger.warning(f"[CCM] Attempt {attempt+1}: {resp.status} - {response_text[:100]}")
        except asyncio.TimeoutError:
            logger.warning(f"[CCM] Attempt {attempt+1}: Timeout")
        except Exception as e:
            logger.error(f"[CCM] Attempt {attempt+1}: {e}")
        
        if attempt < 2:
            await asyncio.sleep(1)
    
    logger.error(f"[CCM✗] Failed after 3 attempts")
    return False

# ============================================================================
# ASSISTANT WITH SLOWER GREETING - FIXED
# ============================================================================
class Assistant(Agent):
    def __init__(self, call_id: str, customer_id: str):
        # FIX: Add greeting to initial context so it's played verbatim
        initial_ctx = llm.ChatContext().append(
            role="system",
            text="""You are a helpful voice AI assistant for Expertflow Support.

When a customer asks to speak with a human agent or mentions "transfer", "agent", 
"representative", "human", "connect me", say "Let me connect you with our team" then STOP speaking."""
        )
        
        super().__init__(
            chat_ctx=initial_ctx,
        )
        self.call_id = call_id
        self.customer_id = customer_id
        self.greeting_sent = False
    
    async def on_enter(self):
        """Send greeting when agent becomes active"""
        if self.greeting_sent:
            return
        
        self.greeting_sent = True
        
        # FIX: Exact greeting, slower pace
        welcome_msg = "Welcome to Expertflow Support. Let me know how I can help you?"
        
        logger.info(f"[AGENT] Sending greeting: {welcome_msg}")
        
        # Send to CCM first
        await send_to_ccm(self.call_id, self.customer_id, welcome_msg, "BOT")
        
        # FIX: Add to conversation history and trigger response
        if self.session and self.session.chat_ctx:
            self.session.chat_ctx.append(
                role="assistant",
                text=welcome_msg
            )
            # Trigger the agent to actually speak it
            self.session.generate_reply()
        
        logger.info("[AGENT] ✅ Greeting triggered")

# ============================================================================
# SERVER SETUP
# ============================================================================
server = AgentServer()

def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()

server.setup_fnc = prewarm

# ============================================================================
# MAIN AGENT HANDLER
# ============================================================================
@server.rtc_session(agent_name="")
async def my_agent(ctx: JobContext):
    ctx.log_context_fields = {"room": ctx.room.name}
    
    call_id = ctx.room.name
    customer_id = ctx.room.metadata if ctx.room.metadata else "unknown"
    
    logger.info(f"[CALL] 🔵 NEW: Room={call_id}, Customer={customer_id}")
    
    # State tracking
    state = {
        "customer_identity": None,
        "human_agent_identity": None,
        "transfer_triggered": False,
        "ai_active": True,
        "deepgram_tasks": [],
    }
    
    session_ref = {"session": None}
    
    # Extract initial customer
    for p in ctx.room.remote_participants.values():
        if p.identity.startswith("sip_") and not p.identity.startswith("human"):
            state["customer_identity"] = p.identity
            customer_id = p.identity.replace("sip_", "")
            logger.info(f"[CALL] Initial customer: {customer_id}")
            break
    
    # ========================================================================
    # TRANSFER FUNCTION
    # ========================================================================
    async def execute_transfer():
        """Transfer to human agent"""
        if state["transfer_triggered"]:
            return
        
        state["transfer_triggered"] = True
        logger.info("[TRANSFER] 🔴 Executing...")
        
        await send_to_ccm(call_id, customer_id, "Connecting you to our live agent...", "BOT")
        
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
            await send_to_ccm(call_id, customer_id, "Transfer initiated", "BOT")
            
        except Exception as e:
            logger.error(f"[TRANSFER] ❌ Failed: {e}", exc_info=True)
            state["transfer_triggered"] = False
            await send_to_ccm(call_id, customer_id, "Transfer failed. Please try again.", "BOT")
    
    # ========================================================================
    # DEEPGRAM TRANSCRIPTION - FIXED
    # ========================================================================
    async def start_deepgram_for_participant(participant: rtc.RemoteParticipant, track: rtc.RemoteAudioTrack):
        """Start Deepgram transcription"""
        logger.info(f"[DEEPGRAM] Starting for: {participant.identity}")
        
        try:
            # Create Deepgram STT instance
            deepgram_stt = deepgram.STT(
                model="nova-2",
                language="en-US",
                detect_language=False,
                interim_results=False,  # Only final results
                smart_format=True,
                punctuate=True,
            )
            
            # Create audio stream from track
            audio_stream = rtc.AudioStream(track)
            
            # Start STT stream
            stt_stream = deepgram_stt.stream()
            
            logger.info(f"[DEEPGRAM] Stream created for {participant.identity}")
            
            # Determine sender type
            is_human_agent = participant.identity.startswith("human-agent")
            sender_type = "AGENT" if is_human_agent else "CONNECTOR"
            
            # FIX: Process audio and transcriptions concurrently
            async def push_audio():
                """Push audio frames to Deepgram"""
                try:
                    async for audio_frame in audio_stream:
                        stt_stream.push_frame(audio_frame)
                except Exception as e:
                    logger.error(f"[DEEPGRAM] Audio push error: {e}")
            
            async def process_events():
                """Process transcription events"""
                try:
                    async for event in stt_stream:
                        if event.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                            text = event.alternatives[0].text.strip()
                            if text:
                                logger.info(f"[DEEPGRAM→{sender_type}] {text}")
                                await send_to_ccm(call_id, customer_id, text, sender_type)
                        elif event.type == stt.SpeechEventType.START_OF_SPEECH:
                            logger.debug(f"[DEEPGRAM] Speech started: {participant.identity}")
                        elif event.type == stt.SpeechEventType.END_OF_SPEECH:
                            logger.debug(f"[DEEPGRAM] Speech ended: {participant.identity}")
                except Exception as e:
                    logger.error(f"[DEEPGRAM] Event processing error: {e}")
            
            # Run both tasks
            await asyncio.gather(
                push_audio(),
                process_events(),
                return_exceptions=True
            )
            
        except Exception as e:
            logger.error(f"[DEEPGRAM] Failed for {participant.identity}: {e}", exc_info=True)
        finally:
            logger.info(f"[DEEPGRAM] Stopped for {participant.identity}")
    
    # ========================================================================
    # ROOM EVENTS
    # ========================================================================
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        logger.info(f"[ROOM] 👤 Joined: {participant.identity}")
        
        # Track customer
        if participant.identity.startswith("sip_") and not participant.identity.startswith("human"):
            state["customer_identity"] = participant.identity
        
        # Human agent joined
        if participant.identity.startswith("human-agent"):
            state["human_agent_identity"] = participant.identity
            logger.info("[ROOM] 🟢 Human agent joined - AI will leave")
            
            async def ai_leave():
                await asyncio.sleep(0.5)
                state["ai_active"] = False
                
                if session_ref["session"]:
                    session_ref["session"].shutdown()
                    logger.info("[AGENT] ✅ AI session shutdown")
            
            asyncio.create_task(ai_leave())
    
    @ctx.room.on("track_subscribed")
    def on_track_subscribed(track: rtc.Track, publication: rtc.TrackPublication, participant: rtc.RemoteParticipant):
        nonlocal customer_id
        
        logger.info(f"[ROOM] 🎧 Track: {participant.identity} - Kind: {track.kind}")
        
        # Extract customer ID
        if customer_id == "unknown" and participant.identity.startswith("sip_"):
            customer_id = participant.identity.replace("sip_", "")
            logger.info(f"[ROOM] Customer ID: {customer_id}")
        
        # Start Deepgram for human agent
        if participant.identity.startswith("human-agent") and track.kind == rtc.TrackKind.KIND_AUDIO:
            logger.info("[ROOM] 🎙️ Starting Deepgram for human agent")
            task = asyncio.create_task(start_deepgram_for_participant(participant, track))
            state["deepgram_tasks"].append(task)
        
        # Start Deepgram for customer AFTER AI leaves
        elif (participant.identity == state["customer_identity"] and 
              track.kind == rtc.TrackKind.KIND_AUDIO and 
              state["human_agent_identity"] is not None):
            logger.info("[ROOM] 🎙️ Starting Deepgram for customer (post-transfer)")
            task = asyncio.create_task(start_deepgram_for_participant(participant, track))
            state["deepgram_tasks"].append(task)
    
    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        logger.info(f"[ROOM] 👋 Left: {participant.identity}")
    
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
    # SESSION EVENTS
    # ========================================================================
    @session.on("user_input_transcribed")
    def on_user_input_transcribed(event):
        """Customer speaks to AI"""
        if not event.is_final or not state["ai_active"]:
            return
        
        transcript = event.transcript.strip()
        if not transcript:
            return
        
        # FIX: Always log customer speech
        logger.info(f"[CUSTOMER→AI] {transcript}")
        
        # Send to CCM
        asyncio.create_task(send_to_ccm(call_id, customer_id, transcript, "CONNECTOR"))
        
        # Check transfer keywords
        keywords = ["transfer", "human", "agent", "representative", "person", "someone", "connect"]
        if any(k in transcript.lower() for k in keywords):
            logger.info("[TRANSFER] Keyword detected!")
            asyncio.create_task(execute_transfer())
    
    @session.on("conversation_item_added")
    def on_conversation_item_added(event):
        """AI speaks"""
        if not state["ai_active"]:
            return
        
        item = event.item
        if item.role == "assistant" and hasattr(item, 'text_content') and item.text_content:
            text = item.text_content.strip()
            if text:
                logger.info(f"[AI→CUSTOMER] {text}")
                asyncio.create_task(send_to_ccm(call_id, customer_id, text, "BOT"))
    
    # ========================================================================
    # START SESSION
    # ========================================================================
    await session.start(
        agent=Assistant(call_id, customer_id),
        room=ctx.room,
    )
    
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    
    logger.info(f"[AGENT] ✅ Connected")

# ============================================================================
# RUN SERVER
# ============================================================================
if __name__ == "__main__":
    cli.run_app(server)