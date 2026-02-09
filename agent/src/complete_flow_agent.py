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
    llm,
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
# TRANSFER FUNCTION DEFINITION
# ============================================================================
transfer_function = llm.FunctionInfo(
    name="transfer_to_agent",
    description="""Transfer the call to a human agent when user requests to speak with someone 
    or needs human assistance. Call this immediately when customer says things like:
    - 'transfer me to agent'
    - 'I want to talk to human'
    - 'connect me to representative'
    - 'speak to someone'
    - 'need help from person'""",
    parameters={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Reason for transfer (e.g., 'customer_request', 'technical_issue', 'complaint')",
                "default": "customer_request"
            },
            "department": {
                "type": "string",
                "description": "Department to transfer to (default: 'general')",
                "default": "general"
            }
        },
        "required": []
    }
)

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
    # Try to get customer ID from room metadata or use "unknown"
    customer_id = ctx.room.metadata if ctx.room.metadata else "unknown"
    
    logger.info(f"🔵 NEW CALL: Room={call_id}, Customer={customer_id}")
    
    # Track if transfer is in progress
    transfer_in_progress = False
    human_agent_participant = None
    
    # ========================================================================
    # FUNCTION CALL HANDLER - TRANSFER TO HUMAN AGENT
    # ========================================================================
    async def handle_transfer(function_call: llm.FunctionCallInfo):
        nonlocal transfer_in_progress, human_agent_participant
        
        logger.info(f"🔴 TRANSFER REQUESTED: {function_call.arguments}")
        
        reason = function_call.arguments.get("reason", "customer_request")
        department = function_call.arguments.get("department", "general")
        
        # Send acknowledgment to CCM
        await send_to_ccm(call_id, customer_id, 
                         "Connecting you to our live agent...", "BOT")
        
        # ====================================================================
        # INITIATE SIP TRANSFER VIA LIVEKIT API
        # ====================================================================
        try:
            transfer_in_progress = True
            
            # Initialize LiveKit API client
            livekit_api = api.LiveKitAPI(
                url=os.getenv("LIVEKIT_URL"),
                api_key=os.getenv("LIVEKIT_API_KEY"),
                api_secret=os.getenv("LIVEKIT_API_SECRET")
            )
            
            # Your outbound trunk configuration
            outbound_trunk_id = "ST_W7jqvDFA2VgG"  # Your trunk ID
            agent_extension = "99900"  # Your FusionPBX extension
            fusionpbx_ip = "192.168.2.24"  # Your FusionPBX IP
            
            # Create SIP call to human agent - joins SAME ROOM as customer
            logger.info(f"📞 Dialing agent: sip:{agent_extension}@{fusionpbx_ip}:5060")
            
            transfer_result = await livekit_api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    # CRITICAL: Same room name keeps customer in same room
                    room_name=call_id,
                    
                    # Outbound trunk to use
                    sip_trunk_id=outbound_trunk_id,
                    
                    # SIP URI to dial (your FusionPBX extension)
                    sip_call_to=f"sip:{agent_extension}@{fusionpbx_ip}:5060",
                    
                    # Participant identity for human agent
                    participant_identity=f"human-agent-{department}",
                    participant_name=f"Human Agent ({department})",
                    
                    # Metadata for tracking
                    participant_metadata=f'{{"reason": "{reason}", "department": "{department}"}}',
                    
                    # Enable noise cancellation for agent
                    krisp_enabled=True,
                    
                    # Wait for agent to answer before returning
                    wait_until_answered=False,
                )
            )
            
            logger.info(f"✅ TRANSFER INITIATED: {transfer_result}")
            
            # Send transfer status to CCM
            await send_to_ccm(call_id, customer_id, 
                             f"Transfer to {department} department initiated", "BOT")
            
            return f"Transfer initiated to {department} department. Agent joining room."
            
        except Exception as e:
            logger.error(f"❌ TRANSFER FAILED: {e}")
            transfer_in_progress = False
            await send_to_ccm(call_id, customer_id, 
                             "Unable to transfer your call. Please try again later.", "BOT")
            return f"Transfer failed: {str(e)}"
    
    # ========================================================================
    # ROOM EVENT LISTENERS - BIDIRECTIONAL TRANSCRIPTION
    # ========================================================================
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        """Track when human agent joins the room"""
        nonlocal human_agent_participant
        
        logger.info(f"👤 PARTICIPANT JOINED: {participant.identity}")
        
        # Check if this is the human agent (SIP participant)
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            human_agent_participant = participant
            logger.info(f"🟢 HUMAN AGENT CONNECTED: {participant.identity}")
    
    @ctx.room.on("track_subscribed")
    def on_track_subscribed(
        track: rtc.Track, 
        publication: rtc.TrackPublication, 
        participant: rtc.RemoteParticipant
    ):
        """Handle audio tracks from participants"""
        logger.info(f"🎧 TRACK SUBSCRIBED: {participant.identity} - {track.kind}")
        
        # If human agent's audio track, set up transcription
        if (participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP and 
            track.kind == rtc.TrackKind.KIND_AUDIO):
            logger.info(f"🎤 Setting up human agent audio transcription")
            # LiveKit will automatically handle audio mixing
            # Transcription is handled by the session events below
    
    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        """Track when participants leave"""
        logger.info(f"👋 PARTICIPANT LEFT: {participant.identity}")
    
    # ========================================================================
    # OPENAI REALTIME SESSION WITH FUNCTION CALLING
    # ========================================================================
    session = AgentSession(
        llm=openai.realtime.RealtimeModel(
            # Model selection
            model="gpt-4o-realtime-preview-2024-12-17",
            
            # Voice selection
            voice="alloy",
            
            # Temperature
            temperature=0.8,
            
            # Modalities: audio for speech-to-speech
            modalities=['text', 'audio'],
            
            # Turn detection
            turn_detection={
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 500,
            },
        ),
        
        # VAD
        vad=ctx.proc.userdata["vad"],
        
        # Function context for transfer
        fnc_ctx=llm.FunctionContext(),
    )
    
    # Register transfer function
    session.fnc_ctx.ai_callable(transfer_function)(handle_transfer)
    
    # ========================================================================
    # SESSION EVENT HANDLERS - SEND TRANSCRIPTS TO CCM
    # ========================================================================
    @session.on("user_speech_committed")
    def on_user_speech(msg: llm.ChatMessage):
        """Customer speech → Send to CCM"""
        if msg.content:
            logger.info(f"👤 USER: {msg.content}")
            # Run async function in background
            ctx._loop.create_task(
                send_to_ccm(call_id, customer_id, msg.content, "CONNECTOR")
            )
    
    @session.on("agent_speech_committed")
    def on_agent_speech(msg: llm.ChatMessage):
        """AI agent speech → Send to CCM"""
        if msg.content:
            logger.info(f"🤖 AGENT: {msg.content}")
            # Run async function in background
            ctx._loop.create_task(
                send_to_ccm(call_id, customer_id, msg.content, "BOT")
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