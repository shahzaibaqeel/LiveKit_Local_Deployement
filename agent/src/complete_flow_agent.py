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

# ENV
current_dir = Path(__file__).parent
load_dotenv(current_dir / ".env", override=True)

logger = logging.getLogger("agent")
logger.setLevel(logging.INFO)

# ------------------------------------------------------------------------
# CCM
# ------------------------------------------------------------------------
async def send_to_ccm(call_id, customer_id, message, sender):
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
                "id": sender,
                "type": sender,
                "senderName": sender
            },
            "timestamp": str(int(time.time() * 1000)),
        },
        "body": {"type": "PLAIN", "markdownText": message}
    }

    async with aiohttp.ClientSession() as s:
        try:
            async with s.post(
                "https://efcx4-voice.expertflow.com/ccm/message/receive",
                json=payload
            ):
                logger.info(f"[CCM] {sender}: {message}")
        except Exception as e:
            logger.error(f"[CCM] ERROR: {e}")

# ------------------------------------------------------------------------
# AI
# ------------------------------------------------------------------------
class Assistant(Agent):
    def __init__(self, call_id, customer_id):
        super().__init__(
            instructions="""
You are Expertflow voice assistant.
If user asks for agent, say:
"Let me connect you with our team"
Then STOP.
"""
        )
        self.call_id = call_id
        self.customer_id = customer_id
        self.greeted = False

    async def on_enter(self):
        if self.greeted:
            return
        self.greeted = True
        msg = "Welcome to Expertflow Support, how can I help you?"
        await send_to_ccm(self.call_id, self.customer_id, msg, "BOT")
        self.session.generate_reply(
            instructions=f'Say exactly: "{msg}"'
        )

# ------------------------------------------------------------------------
# SERVER
# ------------------------------------------------------------------------
server = AgentServer()

def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()

server.setup_fnc = prewarm

# ------------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------------
@server.rtc_session(agent_name="")
async def my_agent(ctx: JobContext):
    call_id = ctx.room.name
    customer_id = "unknown"

    state = {
        "ai_active": True,
        "customer_identity": None,
        "human_identity": None,
        "call_ended": False,
    }

    session_ref = {"session": None}

    # ------------------------------------------------------------------
    # HANGUP LOGIC
    # ------------------------------------------------------------------
    async def end_call(reason):
        if state["call_ended"]:
            return
        state["call_ended"] = True
        logger.info(f"[CALL END] {reason}")

        if session_ref["session"]:
            session_ref["session"].shutdown()

        lk = api.LiveKitAPI(
            url=os.getenv("LIVEKIT_URL"),
            api_key=os.getenv("LIVEKIT_API_KEY"),
            api_secret=os.getenv("LIVEKIT_API_SECRET")
        )

        for p in [state["customer_identity"], state["human_identity"]]:
            if p:
                try:
                    await lk.room.remove_participant(call_id, p)
                except:
                    pass

    # ------------------------------------------------------------------
    # TRANSFER
    # ------------------------------------------------------------------
    async def execute_transfer():
        await send_to_ccm(call_id, customer_id, "Connecting you to our live agent", "BOT")

        lk = api.LiveKitAPI(
            url=os.getenv("LIVEKIT_URL"),
            api_key=os.getenv("LIVEKIT_API_KEY"),
            api_secret=os.getenv("LIVEKIT_API_SECRET")
        )

        await lk.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                room_name=call_id,
                sip_trunk_id="ST_W7jqvDFA2VgG",
                sip_call_to="99900",
                participant_identity=f"human-{customer_id}",
                participant_name="Human Agent"
            )
        )

    # ------------------------------------------------------------------
    # ROOM EVENTS
    # ------------------------------------------------------------------
    @ctx.room.on("participant_connected")
    def on_join(p):
        logger.info(f"[JOIN] {p.identity}")

        if p.identity.startswith("sip_"):
            state["customer_identity"] = p.identity

        if p.identity.startswith("human-"):
            state["human_identity"] = p.identity
            state["ai_active"] = False
            logger.info("[AI] Muted, STT-only mode")

    @ctx.room.on("participant_disconnected")
    def on_leave(p):
        logger.info(f"[LEAVE] {p.identity}")
        asyncio.create_task(end_call("Participant left"))

    # ------------------------------------------------------------------
    # OPENAI SESSION
    # ------------------------------------------------------------------
    session = AgentSession(
        llm=openai.realtime.RealtimeModel(
            model="gpt-4o-realtime-preview-2024-12-17",
            voice="alloy",
            modalities=["text", "audio"],
            turn_detection={"type": "server_vad"},
        ),
        vad=ctx.proc.userdata["vad"],
    )

    session_ref["session"] = session

    # ------------------------------------------------------------------
    # STT EVENTS (WORK BOTH BEFORE & AFTER TRANSFER)
    # ------------------------------------------------------------------
    @session.on("user_input_transcribed")
    def on_stt(event):
        if not event.is_final:
            return

        text = event.transcript.strip()
        if not text:
            return

        sender = "CONNECTOR"
        if state["human_identity"]:
            sender = "AGENT"

        asyncio.create_task(send_to_ccm(call_id, customer_id, text, sender))

        if state["ai_active"]:
            if any(k in text.lower() for k in ["agent", "human", "transfer"]):
                asyncio.create_task(execute_transfer())

    @session.on("conversation_item_added")
    def on_ai_speak(event):
        if not state["ai_active"]:
            return

        item = event.item
        if item.role == "assistant" and item.text_content:
            asyncio.create_task(send_to_ccm(call_id, customer_id, item.text_content, "BOT"))

    # ------------------------------------------------------------------
    await session.start(agent=Assistant(call_id, customer_id), room=ctx.room)
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    logger.info("[READY]")

# ------------------------------------------------------------------------
if __name__ == "__main__":
    cli.run_app(server)
