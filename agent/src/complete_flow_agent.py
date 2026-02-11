"""
===================================================================================
LIVEKIT AGENT WITH OPENAI REALTIME API + CALL TRANSFER + FULL TRANSCRIPTION
TRANSFER FIXED - PRODUCTION READY
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
)
from livekit.plugins import silero
from livekit.plugins import openai

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
    """Send transcript to CCM API"""
    logger.info(f"[CCM] Sending {sender_type}: {message[:50]}...")
    
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
        "body": {
            "type": "PLAIN",
            "markdownText": message
        }
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://efcx4-voice.expertflow.com/ccm/message/receive",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status in [200, 202]:
                    logger.info(f"[CCM] ✅ Success ({resp.status}): {sender_type}")
                else:
                    logger.error(f"[CCM] ❌ Failed: {resp.status}")
                    error_text = await resp.text()
                    logger.error(f"[CCM] Error: {error_text}")
    except Exception as e:
        logger.error(f"[CCM] ❌ Error: {e}")

# ============================================================================
# AGENT DEFINITION WITH EXACT GREETING
# ============================================================================
class Assistant(Agent):
    def __init__(self, call_id: str, customer_id: str) -> None:
        super().__init__(
            instructions="""You are a helpful voice AI assistant for Expertflow Support.

When a customer asks to speak with a human agent or mentions "transfer", "agent", 
"representative", "human", "connect me", say "Let me connect you with our team" then STOP speaking.""",
        )
        self.call_id = call_id
        self.customer_id = customer_id
        self.greeting_sent = False
    
    async def on_enter(self):
        """Called when agent becomes active - Send exact greeting"""
        if self.greeting_sent:
            return
        
        self.greeting_sent = True
        logger.info(f"[AGENT] on_enter() called - Sending exact greeting")
        
        welcome_msg = "Welcome to Expertflow Support, let me know how I can help you?"
        await send_to_ccm(self.call_id, self.customer_id, welcome_msg, "BOT")
        
        self.session.generate_reply(
            instructions=f'Say EXACTLY this and nothing else: "{welcome_msg}"'
        )
        
        logger.info(f"[AGENT] ✅ Exact greeting triggered")

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
    ctx.log_context_fields = {"room": ctx.room.name}
    
    call_id = ctx.room.name
    customer_id = ctx.room.metadata if ctx.room.metadata else "unknown"
    
    logger.info(f"[CALL] 🔵 NEW: Room={call_id}, Customer={customer_id}")
    
    # State tracking
    customer_identity = {"value": None}
    human_agent_identity = {"value": None}
    transfer_triggered = {"value": False}
    session_ref = {"session": None}
    ai_active = {"value": True}
    
    # Extract customer ID from existing participants
    for participant in ctx.room.remote_participants.values():
        if participant.identity.startswith("sip_") and not participant.identity.startswith("human"):
            customer_id = participant.identity.replace("sip_", "")
            customer_identity["value"] = participant.identity
            logger.info(f"[CALL] Customer: {customer_id}")
            break
    
    # ========================================================================
    # DISCONNECT CALL
    # ========================================================================
    async def disconnect_call(reason: str):
        """End entire call"""
        logger.info(f"[CALL] 🔴 Ending - {reason}")
        
        try:
            livekit_api = api.LiveKitAPI(
                url=os.getenv("LIVEKIT_URL"),
                api_key=os.getenv("LIVEKIT_API_KEY"),
                api_secret=os.getenv("LIVEKIT_API_SECRET")
            )
            
            # Remove all participants
            for identity in [customer_identity["value"], human_agent_identity["value"]]:
                if identity:
                    try:
                        await livekit_api.room.remove_participant(room=call_id, identity=identity)
                        logger.info(f"[CALL] Removed: {identity}")
                    except:
                        pass
            
            logger.info(f"[CALL] ✅ Ended")
        except Exception as e:
            logger.error(f"[CALL] Error: {e}")
    
    # ========================================================================
    # TRANSFER - FIXED: Removed enable_krisp parameter
    # ========================================================================
    async def execute_transfer():
        """Transfer to human agent"""
        if transfer_triggered["value"]:
            return
            
        transfer_triggered["value"] = True
        logger.info(f"[TRANSFER] 🔴 EXECUTING")
        
        await send_to_ccm(call_id, customer_id, "Connecting you to our live agent...", "BOT")
        
        try:
            livekit_api = api.LiveKitAPI(
                url=os.getenv("LIVEKIT_URL"),
                api_key=os.getenv("LIVEKIT_API_KEY"),
                api_secret=os.getenv("LIVEKIT_API_SECRET")
            )
            
            # FIX: Removed enable_krisp - it doesn't exist in the API
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
            transfer_triggered["value"] = False
    
    # ========================================================================
    # ROOM EVENTS
    # ========================================================================
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        logger.info(f"[ROOM] 👤 Joined: {participant.identity}")
        
        # Track customer
        if participant.identity.startswith("sip_") and not participant.identity.startswith("human"):
            customer_identity["value"] = participant.identity
        
        # Human agent joined - AI leaves
        if participant.identity.startswith("human-agent"):
            human_agent_identity["value"] = participant.identity
            logger.info(f"[ROOM] 🟢 Human agent connected")
            
            async def ai_leave():
                await asyncio.sleep(0.5)
                ai_active["value"] = False
                logger.info("[AGENT] 🤖 AI leaving...")
                
                if session_ref["session"]:
                    session_ref["session"].shutdown()
                    logger.info("[AGENT] ✅ AI session shutdown complete")
            
            asyncio.create_task(ai_leave())
    
    @ctx.room.on("track_subscribed")
    def on_track_subscribed(track: rtc.Track, publication: rtc.TrackPublication, participant: rtc.RemoteParticipant):
        logger.info(f"[ROOM] 🎧 Track: {participant.identity} - {track.kind}")
        
        # Extract customer ID
        nonlocal customer_id
        if customer_id == "unknown" and participant.identity.startswith("sip_"):
            customer_id = participant.identity.replace("sip_", "")
            logger.info(f"[ROOM] Customer ID: {customer_id}")
    
    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        logger.info(f"[ROOM] 👋 Left: {participant.identity}")
        
        # Customer left - end call
        if participant.identity == customer_identity["value"]:
            logger.info(f"[ROOM] Customer left - ending call")
            asyncio.create_task(disconnect_call("Customer disconnected"))
        
        # Human agent left - end call
        elif participant.identity == human_agent_identity["value"]:
            logger.info(f"[ROOM] Agent left - ending call")
            asyncio.create_task(disconnect_call("Agent disconnected"))
    
    # ========================================================================
    # SESSION
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
    # USER TRANSCRIPT (Customer speaks)
    # ========================================================================
    @session.on("user_input_transcribed")
    def on_user_input_transcribed(event):
        if not event.is_final or not ai_active["value"]:
            return
        
        transcript = event.transcript
        logger.info(f"[USER] 👤 {transcript}")
        
        # Send to CCM
        asyncio.create_task(send_to_ccm(call_id, customer_id, transcript, "CONNECTOR"))
        
        # Check transfer keywords
        keywords = ["transfer", "human", "agent", "representative", "person", "someone", "connect"]
        if any(k in transcript.lower() for k in keywords):
            logger.info(f"[TRANSFER] Keyword detected")
            asyncio.create_task(execute_transfer())
    
    # ========================================================================
    # AGENT RESPONSE (AI speaks)
    # ========================================================================
    @session.on("conversation_item_added")
    def on_conversation_item_added(event):
        if not ai_active["value"]:
            return
        
        item = event.item
        if item.role == "assistant" and hasattr(item, 'text_content') and item.text_content:
            logger.info(f"[AGENT] 🤖 {item.text_content}")
            asyncio.create_task(send_to_ccm(call_id, customer_id, item.text_content, "BOT"))
    
    # ========================================================================
    # START
    # ========================================================================
    await session.start(
        agent=Assistant(call_id, customer_id),
        room=ctx.room,
    )
    
    await ctx.connect()
    
    logger.info(f"[AGENT] ✅ Connected")

# ============================================================================
# RUN
# ============================================================================
if __name__ == "__main__":
    cli.run_app(server)
