"""
============================================================================
LIVEKIT AGENT WITH OPENAI REALTIME API + CALL TRANSFER TO HUMAN AGENT
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
                "https://cx-voice.expertflow.com/ccm/message/receive",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    logger.info(f"✅ CCM sent: {sender_type}")
    except Exception as e:
        logger.error(f"❌ CCM error: {e}")

# ============================================================================
# AGENT DEFINITION
# ============================================================================
class Assistant(Agent):
    def __init__(self, call_id: str, customer_id: str) -> None:
        super().__init__(
            instructions="""You are a helpful voice AI assistant for EW HealthCare.

When a customer asks to speak with a human agent or mentions "transfer", "agent", 
"representative", "human", say "Let me connect you with our team" then STOP speaking.""",
        )
        self.call_id = call_id
        self.customer_id = customer_id

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
    
    logger.info(f"🔵 NEW CALL: Room={call_id}, Customer={customer_id}")
    
    transfer_triggered = {"value": False}
    
    # ========================================================================
    # TRANSFER FUNCTION
    # ========================================================================
    async def execute_transfer():
        """Execute SIP transfer to human agent"""
        if transfer_triggered["value"]:
            logger.info("Transfer already in progress, skipping")
            return
            
        transfer_triggered["value"] = True
        logger.info(f"🔴 EXECUTING TRANSFER")
        
        await send_to_ccm(call_id, customer_id, "Connecting you to our live agent...", "BOT")
        
        try:
            livekit_api = api.LiveKitAPI(
                url=os.getenv("LIVEKIT_URL"),
                api_key=os.getenv("LIVEKIT_API_KEY"),
                api_secret=os.getenv("LIVEKIT_API_SECRET")
            )
            
            outbound_trunk_id = "ST_W7jqvDFA2VgG"
            agent_extension = "99900"
            fusionpbx_ip = "192.168.2.24"
            
            logger.info(f"📞 Calling: sip:{agent_extension}@{fusionpbx_ip}:5060")
            
            transfer_result = await livekit_api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=call_id,
                    sip_trunk_id=outbound_trunk_id,
                    sip_call_to=f"sip:{agent_extension}@{fusionpbx_ip}:5060",
                    participant_identity=f"human-agent-general",
                    participant_name=f"Human Agent",
                    participant_metadata='{"reason": "customer_request"}',
                )
            )
            
            logger.info(f"✅ TRANSFER SUCCESS: Participant ID = {transfer_result.participant_id}")
            logger.info(f"✅ Full response: {transfer_result}")
            await send_to_ccm(call_id, customer_id, "Transfer initiated", "BOT")
            
        except Exception as e:
            logger.error(f"❌ TRANSFER FAILED: {e}", exc_info=True)
            transfer_triggered["value"] = False
            await send_to_ccm(call_id, customer_id, "Transfer failed. Please try again.", "BOT")
    
    # ========================================================================
    # ROOM EVENTS
    # ========================================================================
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        logger.info(f"👤 JOINED: {participant.identity}, Kind: {participant.kind}")
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            logger.info(f"🟢 HUMAN AGENT CONNECTED: {participant.identity}")
    
    @ctx.room.on("track_subscribed")
    def on_track_subscribed(track: rtc.Track, publication: rtc.TrackPublication, participant: rtc.RemoteParticipant):
        logger.info(f"🎧 TRACK: {participant.identity} - {track.kind}")
    
    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        logger.info(f"👋 LEFT: {participant.identity}")
    
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
    
    # ========================================================================
    # CONVERSATION ITEM ADDED EVENT - FIXED: USE asyncio.create_task
    # ========================================================================
    @session.on("conversation_item_added")
    def on_conversation_item_added(event):
        """Called when user OR agent speech is committed to chat history"""
        item = event.item
        role = item.role  # "user" or "assistant"
        
        # Get text content from item
        text_content = None
        if hasattr(item, 'text_content') and item.text_content:
            text_content = item.text_content
        elif hasattr(item, 'content') and item.content:
            # Handle different content types
            for content in item.content:
                if hasattr(content, 'text'):
                    text_content = content.text
                    break
        
        if not text_content:
            return
            
        if role == "user":
            logger.info(f"👤 USER: {text_content}")
            
            # Check for transfer keywords
            transfer_keywords = ["transfer", "human", "agent", "representative", "person", "someone"]
            if any(keyword in text_content.lower() for keyword in transfer_keywords):
                logger.info(f"🔍 TRANSFER KEYWORD DETECTED: '{text_content}'")
                # Execute transfer - FIXED: use asyncio.create_task
                asyncio.create_task(execute_transfer())
            
            # Send to CCM - FIXED: use asyncio.create_task
            asyncio.create_task(send_to_ccm(call_id, customer_id, text_content, "CONNECTOR"))
            
        elif role == "assistant":
            logger.info(f"🤖 AGENT: {text_content}")
            # Send to CCM - FIXED: use asyncio.create_task
            asyncio.create_task(send_to_ccm(call_id, customer_id, text_content, "BOT"))
    
    # Start session
    await session.start(
        agent=Assistant(call_id, customer_id),
        room=ctx.room,
    )
    
    await ctx.connect()
    
    logger.info(f"✅ AGENT CONNECTED TO ROOM: {call_id}")

# ============================================================================
# RUN SERVER
# ============================================================================
if __name__ == "__main__":
    cli.run_app(server)