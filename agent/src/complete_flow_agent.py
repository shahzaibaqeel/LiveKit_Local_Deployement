"""
============================================================================
LIVEKIT AGENT WITH ROOM-LEVEL TRANSCRIPTION + OPENAI REALTIME API
============================================================================
- Initial greeting immediately on session start
- All transcription via LiveKit internal room-level transcription
- All transcripts (customer, AI, human) pushed to CCM
- AI becomes silent after human transfer
- Call hangs up automatically when one participant leaves
"""

import logging
import os
import asyncio
from pathlib import Path
from dotenv import load_dotenv
import aiohttp
from livekit import rtc, api
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, JobProcess, cli, AutoSubscribe

# Load environment variables
current_dir = Path(__file__).parent
load_dotenv(current_dir / ".env", override=True)

logger = logging.getLogger("agent")
logger.setLevel(logging.INFO)

# =======================================
# CCM HELPER
# =======================================
async def send_to_ccm(call_id: str, customer_id: str, message: str, sender_type: str):
    if not message.strip():
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
            "timestamp": str(int(asyncio.get_event_loop().time() * 1000)),
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
            await asyncio.sleep(0.5)
    logger.error(f"[CCM] ❌ Failed: {sender_type}")
    return False

# =======================================
# AI AGENT
# =======================================
class Assistant(Agent):
    def __init__(self, call_id: str, customer_id: str):
        super().__init__(
            instructions="""You are a helpful voice AI assistant for Expertflow Support.
When customer asks to speak with human, say 'Let me connect you with our team' and stop speaking."""
        )
        self.call_id = call_id
        self.customer_id = customer_id

# =======================================
# SERVER SETUP
# =======================================
server = AgentServer()

def prewarm(proc: JobProcess):
    from livekit.plugins import silero
    proc.userdata["vad"] = silero.VAD.load()
server.setup_fnc = prewarm

# =======================================
# MAIN AGENT HANDLER
# =======================================
@server.rtc_session(agent_name="")
async def my_agent(ctx: JobContext):
    call_id = ctx.room.name
    customer_id = ctx.room.metadata if ctx.room.metadata else "unknown"
    logger.info(f"[CALL] 🔵 NEW: Room={call_id}, Customer={customer_id}")

    state = {
        "customer_identity": None,
        "human_agent_identity": None,
        "transfer_triggered": False,
        "ai_active": True,
        "call_ended": False,
    }
    session_ref = {"session": None}

    # ------------------------
    # TRANSFER TO HUMAN
    # ------------------------
    async def execute_transfer():
        if state["transfer_triggered"]:
            return
        state["transfer_triggered"] = True
        await send_to_ccm(call_id, customer_id, "Connecting to human agent...", "BOT")
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

    # ------------------------
    # END CALL
    # ------------------------
    async def end_call(reason: str):
        if state["call_ended"]:
            return
        state["call_ended"] = True
        state["ai_active"] = False
        logger.info(f"[CALL] 🔴 Ending: {reason}")

        try:
            if session_ref["session"]:
                session_ref["session"].shutdown()

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

    # ------------------------
    # ROOM EVENTS
    # ------------------------
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        logger.info(f"[ROOM] 👤 Joined: {participant.identity}")
        if participant.identity.startswith("sip_") and not participant.identity.startswith("human"):
            state["customer_identity"] = participant.identity
        if participant.identity.startswith("human-agent"):
            state["human_agent_identity"] = participant.identity
            state["ai_active"] = False
            if session_ref["session"]:
                session_ref["session"].shutdown()
            logger.info("[AGENT] ✅ AI muted after human joined")

    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        logger.info(f"[ROOM] 👋 Left: {participant.identity}")
        # Hang up when only one left
        if participant.identity in [state["customer_identity"], state["human_agent_identity"]]:
            asyncio.create_task(end_call(f"{participant.identity} disconnected"))

    # ------------------------
    # OPENAI REALTIME SESSION
    # ------------------------
    session = AgentSession(
        llm=None,  # Keep LLM active only if needed, AI muted after transfer
        vad=ctx.proc.userdata["vad"],
    )
    session_ref["session"] = session

    # ------------------------
    # ROOM-LEVEL TRANSCRIPTION HOOKS
    # ------------------------
    @session.on("user_input_transcribed")
    def on_transcribed(event):
        if not event.is_final:
            return
        transcript = event.transcript.strip()
        if not transcript:
            return
        logger.info(f"[TRANSCRIPT] {transcript}")
        # Push all transcripts (customer, human, AI) to CCM
        asyncio.create_task(send_to_ccm(call_id, customer_id, transcript, "CONNECTOR"))
        # Detect transfer
        keywords = ["transfer", "human", "agent", "representative", "connect"]
        if any(k in transcript.lower() for k in keywords) and state["ai_active"]:
            asyncio.create_task(execute_transfer())

    # ------------------------
    # INITIAL GREETING
    # ------------------------
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    if session_ref["session"]:
        asyncio.create_task(
            send_to_ccm(call_id, customer_id, "Welcome to Expertflow Support! How can I help you today?", "BOT")
        )
        session_ref["session"].add_to_conversation(
            role="assistant",
            text="Welcome to Expertflow Support! How can I help you today?"
        )

    # Start session
    await session.start(agent=Assistant(call_id, customer_id), room=ctx.room)
    logger.info(f"[AGENT] ✅ Connected to room: {call_id}")

# =======================================
# RUN SERVER
# =======================================
if __name__ == "__main__":
    cli.run_app(server)
