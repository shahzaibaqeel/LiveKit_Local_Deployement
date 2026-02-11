
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
)
from livekit.plugins import silero
from livekit.plugins import openai
from livekit.agents import stt

# Load environment variables
current_dir = Path(__file__).parent
env_file = current_dir / ".env"
load_dotenv(dotenv_path=env_file, override=True)

logger = logging.getLogger("agent")
logger.setLevel(logging.INFO)


async def send_to_ccm(call_id: str, customer_id: str, message: str, sender_type: str):
    """Send transcript reliably to CCM"""
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
        },
        "body": {"type": "PLAIN", "markdownText": message}
    }
    
    async with aiohttp.ClientSession() as session:
        for attempt in range(3):
            try:
                async with session.post(
                    "https://efcx4-voice.expertflow.com/ccm/message/receive",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status in [200, 202]:
                        logger.info(f"[CCM] ✅ {sender_type}: {message[:50]}...")
                        return
                    else:
                        logger.warning(f"[CCM] Attempt {attempt+1} status {resp.status}")
            except Exception as e:
                logger.error(f"[CCM] Attempt {attempt+1} failed: {e}")
            await asyncio.sleep(1)

# ============================================================================ #
# ASSISTANT WITH GREETING
# ============================================================================ #
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
        if self.greeting_sent:
            return
        self.greeting_sent = True
        welcome_msg = "Welcome to Expertflow Support, let me know how I can help you?"
        logger.info("[AGENT] Sending greeting...")
        await send_to_ccm(self.call_id, self.customer_id, welcome_msg, "BOT")
        if self.session:
            await self.session.say(welcome_msg, allow_interruptions=True)
        logger.info("[AGENT] ✅ Greeting sent")

# ============================================================================ #
# SERVER SETUP
# ============================================================================ #
server = AgentServer()

def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()

server.setup_fnc = prewarm

# ============================================================================ #
# MAIN AGENT HANDLER
# ============================================================================ #
@server.rtc_session(agent_name="")
async def my_agent(ctx: JobContext):
    ctx.log_context_fields = {"room": ctx.room.name}
    
    call_id = ctx.room.name
    customer_id = ctx.room.metadata if ctx.room.metadata else "unknown"
    
    logger.info(f"[CALL] 🔵 NEW Room={call_id}, Customer={customer_id}")
    
    # State
    customer_identity = {"value": None}
    human_agent_identity = {"value": None}
    transfer_triggered = {"value": False}
    session_ref = {"session": None}
    ai_active = {"value": True}
    greeting_played = {"value": False}
    
    # Extract initial customer
    for p in ctx.room.remote_participants.values():
        if p.identity.startswith("sip_") and not p.identity.startswith("human"):
            customer_identity["value"] = p.identity
            customer_id = p.identity.replace("sip_", "")
            logger.info(f"[CALL] Customer: {customer_id}")
            break
    
    # ------------------------------------------------------------------------ #
    # TRANSFER FUNCTION
    # ------------------------------------------------------------------------ #
    async def execute_transfer():
        if transfer_triggered["value"]:
            return
        transfer_triggered["value"] = True
        await send_to_ccm(call_id, customer_id, "Connecting you to live agent...", "BOT")
        
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
            logger.error(f"[TRANSFER] ❌ Failed: {e}")
            transfer_triggered["value"] = False
            await send_to_ccm(call_id, customer_id, "Transfer failed.", "BOT")
    
    # ------------------------------------------------------------------------ #
    # ROOM EVENTS
    # ------------------------------------------------------------------------ #
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        logger.info(f"[ROOM] 👤 Joined: {participant.identity}")
        if participant.identity.startswith("sip_") and not participant.identity.startswith("human"):
            customer_identity["value"] = participant.identity
        
        # AI leaves if human joins
        if participant.identity.startswith("human-agent"):
            human_agent_identity["value"] = participant.identity
            logger.info("[ROOM] 🟢 Human agent joined - AI leaves")
            
            async def ai_leave():
                await asyncio.sleep(0.5)
                ai_active["value"] = False
                if session_ref["session"]:
                    await session_ref["session"].aclose()
                logger.info("[AGENT] ✅ AI session shutdown")
            
            asyncio.create_task(ai_leave())
    
    @ctx.room.on("track_subscribed")
    def on_track_subscribed(track: rtc.Track, publication: rtc.TrackPublication, participant: rtc.RemoteParticipant):
        nonlocal customer_id
        if customer_id == "unknown" and participant.identity.startswith("sip_"):
            customer_id = participant.identity.replace("sip_", "")
            logger.info(f"[ROOM] Customer ID set: {customer_id}")
    
    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        logger.info(f"[ROOM] 👋 Left: {participant.identity}")
    
    # ------------------------------------------------------------------------ #
    # OPENAI REALTIME SESSION
    # ------------------------------------------------------------------------ #
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
    
    # ------------------------------------------------------------------------ #
    # CUSTOMER SPEECH TO CCM
    # ------------------------------------------------------------------------ #
    @session.on("user_input_transcribed")
    async def on_user_input_transcribed(event):
        if not event.is_final or not ai_active["value"]:
            return
        transcript = event.transcript
        logger.info(f"[USER] {transcript}")
        await send_to_ccm(call_id, customer_id, transcript, "CONNECTOR")
        keywords = ["transfer", "human", "agent", "representative", "person", "someone", "connect"]
        if any(k in transcript.lower() for k in keywords):
            asyncio.create_task(execute_transfer())
    
    # ------------------------------------------------------------------------ #
    # AI SPEECH TO CCM
    # ------------------------------------------------------------------------ #
    @session.on("conversation_item_added")
    async def on_conversation_item_added(event):
        if not ai_active["value"]:
            return
        item = event.item
        if item.role == "assistant" and hasattr(item, 'text_content') and item.text_content:
            logger.info(f"[AGENT] {item.text_content}")
            await send_to_ccm(call_id, customer_id, item.text_content, "BOT")
    
    # ------------------------------------------------------------------------ #
    # ROOM-WIDE TRANSCRIPTION (human + customer)
    # ------------------------------------------------------------------------ #
    @ctx.room.on("transcription_received")
    async def on_room_transcription(event):
        text = event.text.strip()
        if text:
            logger.info(f"[TRANSCRIPT] {text}")
            await send_to_ccm(call_id, customer_id, text, "CONNECTOR")
    
    # ------------------------------------------------------------------------ #
    # START SESSION
    # ------------------------------------------------------------------------ #
    await session.start(
        agent=Assistant(call_id, customer_id),
        room=ctx.room,
    )
    
    await ctx.connect()
    logger.info(f"[AGENT] ✅ Connected to room {call_id}")

# ============================================================================ #
# RUN SERVER
# ============================================================================ #
if __name__ == "__main__":
    cli.run_app(server)
