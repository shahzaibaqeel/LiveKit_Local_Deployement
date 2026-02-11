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
)
from livekit.plugins import silero, openai, deepgram

# Load environment variables
current_dir = Path(__file__).parent
env_file = current_dir / ".env"
load_dotenv(dotenv_path=env_file, override=True)

logger = logging.getLogger("agent")
logger.setLevel(logging.INFO)

# ============================================================================ 
# CCM API HELPER
# ============================================================================
async def send_to_ccm(call_id: str, customer_id: str, message: str, sender_type: str):
    """Send transcript to CCM with retry logic"""
    if not message or not message.strip():
        return
    
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
    
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for attempt in range(3):
            try:
                async with session.post(
                    "https://efcx4-voice.expertflow.com/ccm/message/receive",
                    json=payload,
                    headers={"Content-Type": "application/json"}
                ) as resp:
                    if resp.status in [200, 202]:
                        logger.info(f"[CCM] ✅ {sender_type}: {message[:50]}...")
                        return True
                    else:
                        logger.warning(f"[CCM] Attempt {attempt+1}: Status {resp.status}")
            except asyncio.TimeoutError:
                logger.warning(f"[CCM] Attempt {attempt+1}: Timeout")
            except Exception as e:
                logger.error(f"[CCM] Attempt {attempt+1}: {e}")
            
            if attempt < 2:
                await asyncio.sleep(1)
    
    logger.error(f"[CCM] ❌ Failed after 3 attempts: {sender_type}")
    return False

# ============================================================================ 
# ASSISTANT WITH GREETING
# ============================================================================
class Assistant(Agent):
    def __init__(self, call_id: str, customer_id: str):
        super().__init__(
            instructions="""You are a helpful voice AI assistant for Expertflow Support.

            When a customer asks to speak with a human agent or mentions "transfer", "agent", 
"representative", "human", "connect me", say "Let me connect you with our team" then STOP speaking."""
        )
        self.call_id = call_id
        self.customer_id = customer_id
        self.greeting_sent = False
    
    async def on_enter(self):
        """Send greeting when agent becomes active"""
        if self.greeting_sent:
            return
        
        self.greeting_sent = True
        welcome_msg = "Welcome to Expertflow Support, let me know how I can help you?"
        
        logger.info("[AGENT] Sending greeting...")
        
        # Send to CCM
        await send_to_ccm(self.call_id, self.customer_id, welcome_msg, "BOT")
        
        # Use generate_reply() for Realtime API
        if self.session:
            self.session.generate_reply(
                instructions=f'Say EXACTLY: "{welcome_msg}"'
            )
        
        logger.info("[AGENT] ✅ Greeting sent")

# ============================================================================ 
# SERVER SETUP
# ============================================================================
server = AgentServer()

def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()
    # Preload Deepgram for faster startup
    if os.getenv("DEEPGRAM_API_KEY"):
        proc.userdata["deepgram"] = deepgram.STT()

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
        "deepgram_stream": None,
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
    # ============================================================================
    async def execute_transfer():
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
    # DEEPGRAM TRANSCRIPTION
    # ============================================================================
    async def start_deepgram_for_participant(participant: rtc.RemoteParticipant, track: rtc.RemoteAudioTrack):
        logger.info(f"[DEEPGRAM] Starting for: {participant.identity}")
        try:
            deepgram_stt = ctx.proc.userdata.get("deepgram") or deepgram.STT()
            audio_stream = rtc.AudioStream(track)
            stt_stream = deepgram_stt.stream()
            state["deepgram_stream"] = stt_stream

            async def process_transcriptions():
                async for event in stt_stream:
                    if event.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                        text = event.alternatives[0].text.strip()
                        if text:
                            sender_type = "AGENT" if participant.identity.startswith("human-agent") else "CONNECTOR"
                            logger.info(f"[{sender_type}] {text}")
                            await send_to_ccm(call_id, customer_id, text, sender_type)

            async def forward_audio():
                async for audio_frame in audio_stream:
                    if state["deepgram_stream"]:
                        stt_stream.push_frame(audio_frame)

            await asyncio.gather(
                process_transcriptions(),
                forward_audio(),
                return_exceptions=True
            )

        except Exception as e:
            logger.error(f"[DEEPGRAM] Error for {participant.identity}: {e}", exc_info=True)

    # ======================================================================== 
    # ROOM EVENTS - FIX: ALL SYNCHRONOUS
    # ============================================================================
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        logger.info(f"[ROOM] 👤 Joined: {participant.identity}")
        if participant.identity.startswith("sip_") and not participant.identity.startswith("human"):
            state["customer_identity"] = participant.identity
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
        logger.info(f"[ROOM] 🎧 Track: {participant.identity} - {track.kind}")
        if customer_id == "unknown" and participant.identity.startswith("sip_"):
            customer_id = participant.identity.replace("sip_", "")
        if participant.identity.startswith("human-agent") and track.kind == rtc.TrackKind.KIND_AUDIO:
            asyncio.create_task(start_deepgram_for_participant(participant, track))
        elif participant.identity == state["customer_identity"] and track.kind == rtc.TrackKind.KIND_AUDIO and not state["ai_active"]:
            asyncio.create_task(start_deepgram_for_participant(participant, track))

    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        logger.info(f"[ROOM] 👋 Left: {participant.identity}")
        if state["deepgram_stream"]:
            state["deepgram_stream"] = None

        # End call safely
        async def end_call():
            try:
                livekit_api = api.LiveKitAPI(
                    url=os.getenv("LIVEKIT_URL"),
                    api_key=os.getenv("LIVEKIT_API_KEY"),
                    api_secret=os.getenv("LIVEKIT_API_SECRET")
                )
                for pid in [state.get("customer_identity"), state.get("human_agent_identity")]:
                    if pid:
                        try:
                            await livekit_api.room.remove_participant(room=call_id, identity=pid)
                            logger.info(f"[CALL] Removed participant: {pid}")
                        except Exception:
                            pass
                state["ai_active"] = False
                if session_ref["session"]:
                    session_ref["session"].shutdown()
                    logger.info("[AGENT] ✅ AI session shutdown complete")
                logger.info(f"[CALL] ✅ Call ended because {participant.identity} left")
            except Exception as e:
                logger.error(f"[CALL] Error ending call: {e}", exc_info=True)
        asyncio.create_task(end_call())

    # ======================================================================== 
    # OPENAI REALTIME SESSION
    # ============================================================================
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

    @session.on("user_input_transcribed")
    def on_user_input_transcribed(event):
        if not event.is_final or not state["ai_active"]:
            return
        transcript = event.transcript.strip()
        if not transcript:
            return
        logger.info(f"[CUSTOMER] {transcript}")
        asyncio.create_task(send_to_ccm(call_id, customer_id, transcript, "CONNECTOR"))
        keywords = ["transfer", "human", "agent", "representative", "person", "someone", "connect"]
        if any(k in transcript.lower() for k in keywords):
            logger.info("[TRANSFER] Keyword detected")
            asyncio.create_task(execute_transfer())

    @session.on("conversation_item_added")
    def on_conversation_item_added(event):
        if not state["ai_active"]:
            return
        item = event.item
        if item.role == "assistant" and hasattr(item, 'text_content') and item.text_content:
            text = item.text_content.strip()
            if text:
                logger.info(f"[AI-AGENT] {text}")
                asyncio.create_task(send_to_ccm(call_id, customer_id, text, "BOT"))

    # ======================================================================== 
    # START SESSION
    # ============================================================================
    await session.start(
        agent=Assistant(call_id, customer_id),
        room=ctx.room,
    )
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    logger.info(f"[AGENT] ✅ Connected to room: {call_id}")

# ============================================================================ 
# RUN SERVER
# ============================================================================
if __name__ == "__main__":
    cli.run_app(server)