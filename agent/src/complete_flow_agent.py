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
            except Exception as e:
                logger.warning(f"[CCM] Attempt {attempt+1}: {e}")
            
            if attempt < 2:
                await asyncio.sleep(0.5)
    
    logger.error(f"[CCM] ❌ Failed: {sender_type}")
    return False

# ============================================================================ 
# ASSISTANT
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
        "customer_track": None,
        "human_agent_identity": None,
        "transfer_triggered": False,
        "ai_active": True,
        "greeting_sent": False,
        "call_ended": False,
        "forward_task": None,
        "process_task": None,
    }
    
    session_ref = {"session": None}
    
    # ======================================================================== 
    # TRANSFER FUNCTION
    # ========================================================================
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
    
    # ======================================================================== 
    # DEEPGRAM TRANSCRIPTION - FIX: Proper async handling
    # ========================================================================
    async def start_deepgram_transcription():
        """Start Deepgram for customer BEFORE AI leaves"""
        if state["forward_task"] or not state["customer_track"]:
            return
        
        logger.info("[DEEPGRAM] Starting for customer")
        
        try:
            deepgram_stt = deepgram.STT(
                model="nova-2-general",
                language="en-US",
            )
            
            audio_stream = rtc.AudioStream(state["customer_track"])
            stt_stream = deepgram_stt.stream()
            
            async def forward_audio():
                """Forward audio to Deepgram"""
                frame_count = 0
                async for audio_frame in audio_stream:
                    if state["call_ended"]:
                        break
                    stt_stream.push_frame(audio_frame)
                    frame_count += 1
                    if frame_count % 100 == 0:
                        logger.debug(f"[DEEPGRAM] Frames: {frame_count}")
                logger.info(f"[DEEPGRAM] Audio forward stopped. Total frames: {frame_count}")
            
            async def process_transcripts():
                """Process Deepgram transcripts"""
                async for event in stt_stream:
                    if state["call_ended"]:
                        break
                    if event.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                        text = event.alternatives[0].text.strip()
                        if text:
                            logger.info(f"[DEEPGRAM] {text}")
                            await send_to_ccm(call_id, customer_id, text, "CONNECTOR")
                logger.info("[DEEPGRAM] Transcript processing stopped")
            
            # Store tasks separately
            state["forward_task"] = asyncio.create_task(forward_audio())
            state["process_task"] = asyncio.create_task(process_transcripts())
            
            logger.info("[DEEPGRAM] ✅ Started")
            
        except Exception as e:
            logger.error(f"[DEEPGRAM] Error: {e}", exc_info=True)
    
    # ======================================================================== 
    # END CALL FUNCTION - FIX: Proper cleanup
    # ========================================================================
    async def end_call(reason: str):
        """End the entire call"""
        if state["call_ended"]:
            return
        
        state["call_ended"] = True
        state["ai_active"] = False
        
        logger.info(f"[CALL] 🔴 Ending - {reason}")
        
        try:
            # Cancel Deepgram tasks
            if state["forward_task"]:
                state["forward_task"].cancel()
            if state["process_task"]:
                state["process_task"].cancel()
            
            # Shutdown AI session
            if session_ref["session"]:
                session_ref["session"].shutdown()
            
            # Remove all participants
            livekit_api = api.LiveKitAPI(
                url=os.getenv("LIVEKIT_URL"),
                api_key=os.getenv("LIVEKIT_API_KEY"),
                api_secret=os.getenv("LIVEKIT_API_SECRET")
            )
            
            for identity in [state["customer_identity"], state["human_agent_identity"]]:
                if identity:
                    try:
                        await livekit_api.room.remove_participant(room=call_id, identity=identity)
                        logger.info(f"[CALL] Removed: {identity}")
                    except:
                        pass
            
            logger.info("[CALL] ✅ Call ended")
            
        except Exception as e:
            logger.error(f"[CALL] Error: {e}")
    
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
            logger.info("[ROOM] 🟢 Human agent - AI will leave")
            
            async def ai_leave():
                await asyncio.sleep(1)
                state["ai_active"] = False
                if session_ref["session"]:
                    session_ref["session"].shutdown()
                logger.info("[AGENT] ✅ AI left")
            
            asyncio.create_task(ai_leave())
    
    @ctx.room.on("track_subscribed")
    def on_track_subscribed(track: rtc.Track, publication: rtc.TrackPublication, participant: rtc.RemoteParticipant):
        nonlocal customer_id
        logger.info(f"[ROOM] 🎧 Track: {participant.identity} - {track.kind}")
        
        # Update customer ID
        if customer_id == "unknown" and participant.identity.startswith("sip_"):
            customer_id = participant.identity.replace("sip_", "")
        
        # Store customer audio track
        if (participant.identity.startswith("sip_") and 
            not participant.identity.startswith("human") and 
            track.kind == rtc.TrackKind.KIND_AUDIO):
            state["customer_track"] = track
            logger.info("[ROOM] ✅ Customer track stored")
            
            # Start Deepgram
            asyncio.create_task(start_deepgram_transcription())
        
        # Send greeting - FIX: Use OpenAI Realtime, not session.say()
        if track.kind == rtc.TrackKind.KIND_AUDIO and not state["greeting_sent"]:
            state["greeting_sent"] = True
            
            async def send_greeting():
                await asyncio.sleep(1)
                welcome = "Welcome to Expertflow Support, let me know how I can help you?"
                logger.info("[GREETING] Sending...")
                await send_to_ccm(call_id, customer_id, welcome, "BOT")
                
                # Trigger OpenAI to speak the greeting
                if session_ref["session"] and state["ai_active"]:
                    session_ref["session"].conversation.item.create(
                        role="assistant",
                        content=[{"type": "input_text", "text": welcome}]
                    )
                    session_ref["session"].response.create()
            
            asyncio.create_task(send_greeting())
    
    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        logger.info(f"[ROOM] 👋 Left: {participant.identity}")
        
        # End call when anyone leaves
        if participant.identity in [state["customer_identity"], state["human_agent_identity"]]:
            asyncio.create_task(end_call(f"{participant.identity} left"))
    
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
    
    # AI session events
    @session.on("user_input_transcribed")
    def on_user_input_transcribed(event):
        if not event.is_final or not state["ai_active"]:
            return
        
        transcript = event.transcript.strip()
        if not transcript:
            return
        
        logger.info(f"[CUSTOMER-AI] {transcript}")
        asyncio.create_task(send_to_ccm(call_id, customer_id, transcript, "CONNECTOR"))
        
        # Check transfer
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
                logger.info(f"[AI-BOT] {text}")
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