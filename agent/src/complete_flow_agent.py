"""
============================================================================
LIVEKIT AGENT WITH OPENAI REALTIME API + CALL TRANSFER TO HUMAN AGENT
(FIXED: correct event hook for Realtime API)
============================================================================
"""

import logging
import os
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
)
from livekit.plugins import silero, openai

# Load environment variables
current_dir = Path(__file__).parent
env_file = current_dir / ".env"
load_dotenv(dotenv_path=env_file, override=True)

logger = logging.getLogger("agent")
logger.setLevel(logging.INFO)

# =====================================================================
# CCM API HELPER
# =====================================================================
async def send_to_ccm(call_id: str, customer_id: str, message: str, sender_type: str):
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
            "timestamp": str(int(time.time() * 1000)),
        },
        "body": {
            "type": "PLAIN",
            "markdownText": message
        }
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                "https://cx-voice.expertflow.com/ccm/message/receive",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
    except Exception as e:
        logger.error(f"CCM error: {e}")

# =====================================================================
# AGENT DEFINITION
# =====================================================================
class Assistant(Agent):
    def __init__(self, call_id: str, customer_id: str) -> None:
        super().__init__(
            instructions="""You are a helpful voice AI assistant.
When the user asks for a human agent, say you are connecting them."""
        )
        self.call_id = call_id
        self.customer_id = customer_id

# =====================================================================
# SERVER SETUP
# =====================================================================
server = AgentServer()

def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()

server.setup_fnc = prewarm

# =====================================================================
# MAIN AGENT HANDLER
# =====================================================================
@server.rtc_session(agent_name="")
async def my_agent(ctx: JobContext):
    call_id = ctx.room.name
    customer_id = ctx.room.metadata or "unknown"

    logger.info(f"NEW CALL: {call_id}")

    # -----------------------------------------------------------------
    # TRANSFER FUNCTION
    # -----------------------------------------------------------------
    async def execute_transfer():
        logger.error("🔥🔥🔥 TRANSFER FUNCTION CALLED 🔥🔥🔥")

        livekit_api = api.LiveKitAPI(
            url=os.getenv("LIVEKIT_URL"),
            api_key=os.getenv("LIVEKIT_API_KEY"),
            api_secret=os.getenv("LIVEKIT_API_SECRET")
        )

        await livekit_api.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                room_name=call_id,
                sip_trunk_id="ST_W7jqvDFA2VgG",  # outbound trunk
                sip_call_to="sip:99900@192.168.2.24",
                participant_identity="human-agent",
                participant_name="Human Agent",
            )
        )

        logger.error("🚨 SIP INVITE SENT TO FUSION 🚨")

    # -----------------------------------------------------------------
    # OPENAI REALTIME SESSION
    # -----------------------------------------------------------------
    session = AgentSession(
        llm=openai.realtime.RealtimeModel(
            model="gpt-4o-realtime-preview-2024-12-17",
            voice="alloy",
            modalities=["text", "audio"],
        ),
        vad=ctx.proc.userdata["vad"],
    )

    # =================================================================
    # THIS IS THE ONLY REAL FIX
    # =================================================================
    @session.on("conversation.message")
    def on_message(msg):
        if msg.role == "user":
            text = msg.content[0].get("text", "")
            logger.info(f"USER SAID: {text}")

            if "agent" in text.lower() or "human" in text.lower():
                ctx._loop.create_task(execute_transfer())

    # -----------------------------------------------------------------
    # HUMAN JOINS -> BOT LEAVES
    # -----------------------------------------------------------------
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            logger.info("HUMAN AGENT CONNECTED, BOT DISCONNECTING")
            ctx._loop.create_task(ctx.room.local_participant.disconnect())

    # Start
    await session.start(
        agent=Assistant(call_id, customer_id),
        room=ctx.room,
    )
    
    await ctx.connect()
    logger.info("AGENT CONNECTED")

# =====================================================================
# RUN SERVER
# =====================================================================
if __name__ == "__main__":
    cli.run_app(server)
