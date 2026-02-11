"""
============================================================================
LIVEKIT AGENT WITH ROOM-LEVEL TRANSCRIPTION + OPENAI REALTIME API
============================================================================
Features:
- AI greeting immediately when session starts
- All transcription via LiveKit internal room-level STT
- Pushes all transcripts (customer, AI, human agent) to CCM
- AI becomes silent after transfer to human agent
- Call ends cleanly when one participant disconnects
============================================================================
"""

import logging
import os
import asyncio
import time
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

# ---------------------------------------------------------------------------
# Load environment variables
# ---------------------------------------------------------------------------
current_dir = Path(__file__).parent
env_file = current_dir / ".env"
load_dotenv(dotenv_path=env_file, override=True)

logger = logging.getLogger("agent")
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# CCM helper
# ---------------------------------------------------------------------------
async def send_to_ccm(call_id: str, customer_id: str, message: str, sender_type: str):
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
            },
            "timestamp": str(int(time.time() * 1000)),
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
                        logger.warning(f"[CCM] Status {resp.status}: {text}")
            except Exception as e:
                logger.warning(f"[CCM] Attempt {attempt+1}: {e}")
            if attempt < 2:
                await asyncio.sleep(0.5)
    logger.error(f"[CCM] ❌ Failed: {sender_type}")
    return False

# ---------------------------------------------------------------------------
# Assistant (AI)
# ---------------------------------------------------------------------------
class Assistant(Agent):
    def __init__(self, call_id: str, customer_id: str):
        super().__init__(instructions="""
        You are a helpful voice AI assistant for Expertflow Support.
        When customer asks for human agent or mentions "transfer", "agent",
        "representative", "human", "connect me", say "Let me connect you with our team" and stop speaking.
        """)
        self.call_id = call_id
        self.customer_id = customer_id

# ---------------------------------------------------------------------------
# Server setup
# ---------------------------------------------------------------------------
server = AgentServer()

def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()

server.setup_fnc = prewarm

# ---------------------------------------------------------------------------
# Main agent handler
# ---------------------------------------------------------------------------
@server.rtc_session(agent_name="")
async def my_agent(ctx: JobContext):
    call_id = ctx.room.name
    customer_id = ctx.room.metadata or "unknown"

    logger.info(f"[CALL] 🔵 NEW Room={call_id}, Customer={customer_id}")

    state = {
        "customer_identity": None,
        "human_agent_identity": None,
        "ai_active": True,
        "transfer_triggered": False,
        "call_ended": False,
    }

    session_ref = {"session": None}

    # ---------------------------
    # Transfer to human agent
    # ---------------------------
    async def execute_transfer():
        if state["transfer_triggered"]:
            return
        state["transfer_triggered"] = True
        logger.info("[TRANSFER] 🔴 Executing...")
        await send_to_ccm(call_id, customer_id, "Connecting you to human agent...", "BOT")
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
            # Mute AI fully
            state["ai_active"] = False
            if session_ref["session"]:
                session_ref["session"].shutdown()
        except Exception as e:
            logger.error(f"[TRANSFER] ❌ Failed: {e}", exc_info=True)
            state["transfer_triggered"] = False

    # ---------------------------
    # End call when one participant remains
    # ---------------------------
    async def end_call(reason: str):
        if state["call_ended"]:
            return
        state["call_ended"] = True
        logger.info(f"[CALL] 🔴 Ending - {reason}")
        try:
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
            logger.info("[CALL] ✅ Ended cleanly")
        except Exception as e:
            logger.error(f"[CALL] Error: {e}")

    # ---------------------------
    # Room events
    # ---------------------------
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        logger.info(f"[ROOM] 👤 Joined: {participant.identity}")
        if participant.identity.startswith("sip_") and not participant.identity.startswith("human"):
            state["customer_identity"] = participant.identity
        if participant.identity.startswith("human-agent"):
            state["human_agent_identity"] = participant.identity
            logger.info("[ROOM] 🟢 Human agent joined, AI will be silent")
            # AI session already shutdown in execute_transfer

    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        logger.info(f"[ROOM] 👋 Left: {participant.identity}")
        remaining = [p for p in ctx.room.remote_participants.values() if p.identity != participant.identity]
        if len(remaining) <= 1:
            asyncio.create_task(end_call(f"{participant.identity} disconnected"))

    # ---------------------------
    # LiveKit room-level transcription via OpenAI realtime
    # ---------------------------
    session = AgentSession(
        llm=openai.realtime.RealtimeModel(
            model="gpt-4o-realtime-preview-2024-12-17",
            voice="alloy",
            temperature=0.8,
            modalities=['text', 'audio'],
            turn_detection={"type": "server_vad", "threshold": 0.5}
        ),
        vad=ctx.proc.userdata["vad"],
    )
    session_ref["session"] = session

    # Send AI greeting immediately
    await send_to_ccm(call_id, customer_id, "Welcome to Expertflow Support! How can I help you today?", "BOT")
    if session:
        session.generate_reply('Say EXACTLY: "Welcome to Expertflow Support! How can I help you today?"')

    @session.on("user_input_transcribed")
    def on_transcription(event):
        """All room-level transcription comes here"""
        transcript = event.transcript.strip()
        if not transcript:
            return
        sender_type = "CONNECTOR" if event.participant_identity.startswith("sip_") else "AGENT"
        asyncio.create_task(send_to_ccm(call_id, customer_id, transcript, sender_type))
        # Detect transfer keywords
        if state["ai_active"] and any(k in transcript.lower() for k in ["transfer","human","agent","representative"]):
            logger.info("[TRANSFER] Keyword detected in transcription")
            asyncio.create_task(execute_transfer())

    # Start session
    await session.start(agent=Assistant(call_id, customer_id), room=ctx.room)
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    logger.info(f"[AGENT] ✅ Connected to room: {call_id}")

# ---------------------------------------------------------------------------
# Run server
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    cli.run_app(server)
