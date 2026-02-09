"""
============================================================================
LIVEKIT AGENT WITH OPENAI REALTIME API + CALL TRANSFER TO HUMAN AGENT
============================================================================
"""

import logging
import os
import time
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
from livekit.agents.llm import FunctionContext
from livekit.plugins import silero
from livekit.plugins import openai

# Load environment variables
current_dir = Path(__file__).parent
env_file = current_dir / ".env"
load_dotenv(dotenv_path=env_file, override=True)

logger = logging.getLogger("agent")
logger.setLevel(logging.INFO)

# ============================================================================
# CCM API HELPER - Matches your Jambonz implementation
# ============================================================================
async def send_to_ccm(call_id: str, customer_id: str, message: str, sender_type: str):
    """
    Send transcript to CCM API
    sender_type: "BOT" for AI agent, "CONNECTOR" for customer, "AGENT" for human agent
    """
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
                    logger.info(f"✅ CCM message sent: {sender_type} - {message[:50]}")
                else:
                    logger.error(f"❌ CCM failed: {resp.status}")
    except Exception as e:
        logger.error(f"❌ CCM send error: {e}")

# ============================================================================
# AGENT DEFINITION
# ============================================================================
class Assistant(Agent):
    def __init__(self, call_id: str, customer_id: str) -> None:
        super().__init__(
            instructions=f"""You are a helpful voice AI assistant for EW HealthCare.

CRITICAL - TRANSFER DETECTION:
When a customer asks to speak with a human agent, wants to be transferred, mentions "agent", 
"representative", "human", "person", or expresses frustration requiring human intervention, 
you MUST call the transfer_to_agent function IMMEDIATELY.

Common transfer requests:
- "Can I speak to someone?"
- "I want to talk to a human"
- "Transfer me to an agent"
- "I need help from a person"
- "This isn't working, get me someone"
- "Connect me to support"

Before transferring, briefly acknowledge like "I'll connect you with a human agent right away" 
and then call the transfer_to_agent function.

For all other requests, provide helpful assistance concisely.""",
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
@server.rtc_session(agent_name="")  # Empty name = accepts all jobs
async def my_agent(ctx: JobContext):
    ctx.log_context_fields = {"room": ctx.room.name}
    
    # Extract call metadata from room
    call_id = ctx.room.name
    customer_id = ctx.room.metadata if ctx.room.metadata else "unknown"
    
    logger.info(f"🔵 NEW CALL: Room={call_id}, Customer={customer_id}")
    
    # Track transfer state
    transfer_in_progress = False
    human_agent_participant = None
    
    # ========================================================================
    # FUNCTION CALL HANDLER - TRANSFER TO HUMAN AGENT
    # ========================================================================
    async def handle_transfer(reason: str = "customer_request", department: str = "general"):
        """Handle transfer to human agent"""
        nonlocal transfer_in_progress, human_agent_participant
        
        logger.info(f"🔴 TRANSFER REQUESTED: reason={reason}, department={department}")
        
        # Send acknowledgment to CCM
        await send_to_ccm(call_id, customer_id, 
                         "Connecting you to our live agent...", "BOT")
        
        try:
            transfer_in_progress = True
            
            # Initialize LiveKit API client
            livekit_api = api.LiveKitAPI(
                url=os.getenv("LIVEKIT_URL"),
                api_key=os.getenv("LIVEKIT_API_KEY"),
                api_secret=os.getenv("LIVEKIT_API_SECRET")
            )
            
            # Your outbound trunk configuration
            outbound_trunk_id = "ST_W7jqvDFA2VgG"
            agent_extension = "99900"
            fusionpbx_ip = "192.168.2.24"
            
            logger.info(f"📞 Dialing agent: sip:{agent_extension}@{fusionpbx_ip}:5060")
            
            # Create SIP call to human agent - joins SAME ROOM as customer
            transfer_result = await livekit_api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=call_id,  # SAME room as customer
                    sip_trunk_id=outbound_trunk_id,
                    sip_call_to=f"sip:{agent_extension}@{fusionpbx_ip}:5060",
                    participant_identity=f"human-agent-{department}",
                    participant_name=f"Human Agent ({department})",
                    participant_metadata=f'{{"reason": "{reason}", "department": "{department}"}}',
                    krisp_enabled=True,
                )
            )
            
            logger.info(f"✅ TRANSFER INITIATED: {transfer_result}")
            await send_to_ccm(call_id, customer_id, 
                             f"Transfer to {department} department initiated", "BOT")
            
            return f"Transfer initiated to {department} department"
            
        except Exception as e:
            logger.error(f"❌ TRANSFER FAILED: {e}")
            transfer_in_progress = False
            await send_to_ccm(call_id, customer_id, 
                             "Unable to transfer your call. Please try again later.", "BOT")
            return f"Transfer failed: {str(e)}"
    
    # ========================================================================
    # ROOM EVENT LISTENERS
    # ========================================================================
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        """Track when human agent joins"""
        nonlocal human_agent_participant
        logger.info(f"👤 PARTICIPANT JOINED: {participant.identity}")
        
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            human_agent_participant = participant
            logger.info(f"🟢 HUMAN AGENT CONNECTED: {participant.identity}")
    
    @ctx.room.on("track_subscribed")
    def on_track_subscribed(
        track: rtc.Track, 
        publication: rtc.TrackPublication, 
        participant: rtc.RemoteParticipant
    ):
        """Handle audio tracks"""
        logger.info(f"🎧 TRACK SUBSCRIBED: {participant.identity} - {track.kind}")
    
    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        """Track when participants leave"""
        logger.info(f"👋 PARTICIPANT LEFT: {participant.identity}")
    
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
        fnc_ctx=FunctionContext(),
    )
    
    # ========================================================================
    # REGISTER TRANSFER FUNCTION - Using @session.fnc_ctx.ai_callable decorator
    # ========================================================================
    @session.fnc_ctx.ai_callable(
        description="""Transfer the call to a human agent when user requests to speak with someone 
        or needs human assistance. Call this immediately when customer says things like:
        'transfer me to agent', 'I want to talk to human', 'connect me to representative', 
        'speak to someone', 'need help from person'"""
    )
    async def transfer_to_agent(
        reason: str = "customer_request",
        department: str = "general"
    ):
        """
        Transfer call to human agent
        
        Args:
            reason: Reason for transfer (default: 'customer_request')
            department: Department to transfer to (default: 'general')
        """
        return await handle_transfer(reason, department)
    
    # ========================================================================
    # SESSION EVENT HANDLERS - SEND TRANSCRIPTS TO CCM
    # ========================================================================
    @session.on("user_speech_committed")
    def on_user_speech(msg):
        """Customer speech → Send to CCM"""
        text = msg.content if hasattr(msg, 'content') else str(msg)
        if text:
            logger.info(f"👤 USER: {text}")
            ctx._loop.create_task(
                send_to_ccm(call_id, customer_id, text, "CONNECTOR")
            )
    
    @session.on("agent_speech_committed")
    def on_agent_speech(msg):
        """AI agent speech → Send to CCM"""
        text = msg.content if hasattr(msg, 'content') else str(msg)
        if text:
            logger.info(f"🤖 AGENT: {text}")
            ctx._loop.create_task(
                send_to_ccm(call_id, customer_id, text, "BOT")
            )
    
    # Start the agent session
    await session.start(
        agent=Assistant(call_id, customer_id),
        room=ctx.room,
    )
    
    # Connect to the room
    await ctx.connect()
    
    logger.info(f"✅ AGENT CONNECTED TO ROOM: {call_id}")

# ============================================================================
# RUN THE SERVER
# ============================================================================
if __name__ == "__main__":
    cli.run_app(server)