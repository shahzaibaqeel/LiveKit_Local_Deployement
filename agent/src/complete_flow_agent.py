"""
============================================================================
LIVEKIT AGENT WITH OPENAI REALTIME API + ROOM LEVEL TRANSCRIPTION + CALL HANDLING
============================================================================
"""

import logging
import os
import time
import asyncio
from pathlib import Path
from dotenv import load_dotenv
import aiohttp
from livekit import rtc, api
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    cli,
    AutoSubscribe,
)
from livekit.plugins import silero, openai

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
                        text = await resp.text()
                        logger.error(f"[CCM] Status {resp.status}: {text}")
            except Exception as e:
                logger.warning(f"[CCM] Attempt {attempt+1}: {e}")
            
            if attempt < 2:
                await asyncio.sleep(0.5)
    
    logger.error(f"[CCM] ❌ Failed: {sender_type}")
    return False

# ============================================================================ 
# ASSISTANT AGENT
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
        "human_agent_identity": None,
        "transfer_triggered": False,
        "ai_active": True,
        "call_ended": False,
    }
    
    session_ref = {"session": None}

    # ======================================================================== 
    # TRANSFER FUNCTION
    # ======================================================================== 
    async def execute_transfer():
        if state["transfer_triggered"]:
            return
        state["transfer_triggered"] = True
        logger.info("[TRANSFER] 🔴 Executing transfer...")
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
    # END CALL FUNCTION
    # ======================================================================== 
    async def end_call(reason: str):
        if state["call_ended"]:
            return
        state["call_ended"] = True
        state["ai_active"] = False
        logger.info(f"[CALL] 🔴 Ending call - {reason}")
        try:
            # Shutdown AI
            if session_ref["session"]:
                session_ref["session"].shutdown()
            # Remove participants
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
            logger.info("[CALL] ✅ Ended")
        except Exception as e:
            logger.error(f"[CALL] Error: {e}")

    # ======================================================================== 
    # ROOM EVENTS
    # ======================================================================== 
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
                    logger.info("[AGENT] ✅ AI session shutdown, room-level STT continues")
            asyncio.create_task(ai_leave())

    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        logger.info(f"[ROOM] 👋 Left: {participant.identity}")
        if participant.identity == state["customer_identity"]:
            asyncio.create_task(end_call("Customer disconnected"))
        elif participant.identity == state["human_agent_identity"]:
            asyncio.create_task(end_call("Human agent disconnected"))

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

    # ------------------------
    # INITIAL GREETING
    # ------------------------
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    asyncio.create_task(
        send_to_ccm(call_id, customer_id, "Welcome to Expertflow Support! How can I help you today?", "BOT")
    )
    if state["ai_active"]:
        await session_ref["session"].generate_reply(
            "Welcome to Expertflow Support! How can I help you today?"
        )

    # ------------------------
    # ROOM-LEVEL STT HANDLER
    # ------------------------
    @session.on("user_input_transcribed")
    def on_user_input_transcribed(event):
        """Room-level transcription from OpenAI Realtime STT"""
        if not event.is_final:
            return
        transcript = event.transcript.strip()
        if not transcript:
            return
        # Push to CCM
        asyncio.create_task(send_to_ccm(call_id, customer_id, transcript, "CONNECTOR"))
        # Detect transfer keywords
        if any(k in transcript.lower() for k in ["transfer","human","agent","representative","person","someone","connect"]):
            logger.info("[TRANSFER] Keyword detected")
            asyncio.create_task(execute_transfer())

    @session.on("conversation_item_added")
    def on_conversation_item_added(event):
        """AI speaking - push to CCM if AI active"""
        if not state["ai_active"]:
            return
        item = event.item
        if item.role == "assistant" and hasattr(item, 'text_content') and item.text_content:
            text = item.text_content.strip()
            if text:
                asyncio.create_task(send_to_ccm(call_id, customer_id, text, "BOT"))

    # Start session
    await session.start(agent=Assistant(call_id, customer_id), room=ctx.room)
    logger.info(f"[AGENT] ✅ Connected to room: {call_id}")

# ============================================================================ 
# RUN SERVER
# ============================================================================ 
if __name__ == "__main__":
    cli.run_app(server)
