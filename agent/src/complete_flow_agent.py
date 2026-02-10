"""
============================================================================
LIVEKIT AGENT WITH OPENAI REALTIME API + CALL TRANSFER TO HUMAN AGENT
WITH IMMEDIATE GREETING - PRODUCTION READY - FIXED
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
# CCM API HELPER
# ============================================================================
async def send_to_ccm(call_id: str, customer_id: str, message: str, sender_type: str):
    """Send transcript to CCM API"""
    logger.info(f"Sending to CCM - Type: {sender_type}, Message: {message[:50]}...")
    
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
                "https://efcx4-voice.expertflow.com/ccm/message/receive",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    logger.info(f"CCM sent successfully: {sender_type}")
                    response_text = await resp.text()
                    logger.info(f"CCM Response: {response_text[:100]}")
                else:
                    logger.error(f"CCM failed with status: {resp.status}")
                    error_text = await resp.text()
                    logger.error(f"Error details: {error_text}")
    except asyncio.TimeoutError:
        logger.error(f"CCM timeout after 10 seconds")
    except Exception as e:
        logger.error(f"CCM error: {e}", exc_info=True)

# ============================================================================
# AGENT DEFINITION WITH IMMEDIATE GREETING
# ============================================================================
class Assistant(Agent):
    def __init__(self, call_id: str, customer_id: str) -> None:
        # Create initial chat context with system message
        initial_ctx = llm.ChatContext().append(
            role="system",
            text="""You are a helpful voice AI assistant for Expertflow Support.

When a customer asks to speak with a human agent or mentions "transfer", "agent", 
"representative", "human", "connect me", say "Let me connect you with our team" then STOP speaking."""
        )
        
        # Add the greeting as the first assistant message
        initial_ctx.append(
            role="assistant",
            text="Welcome to Expertflow Support, let me know how I can help you?"
        )
        
        super().__init__(
            chat_ctx=initial_ctx,
        )
        self.call_id = call_id
        self.customer_id = customer_id
        self.greeting_sent = False
    
    async def on_enter(self):
        """Called when agent enters the session - Send immediate greeting"""
        logger.info(f"AGENT ON_ENTER CALLED - Sending greeting")
        
        welcome_msg = "Welcome to Expertflow Support, let me know how I can help you?"
        
        # Send to CCM
        await send_to_ccm(self.call_id, self.customer_id, welcome_msg, "BOT")
        
        # THIS IS THE KEY: generate_reply() triggers the agent to speak
        self.session.generate_reply()
        
        self.greeting_sent = True
        logger.info(f"GREETING INITIATED VIA generate_reply()")

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
    
    logger.info(f"NEW CALL: Room={call_id}, Initial Customer={customer_id}")
    
    # Check for existing participants to extract customer_id
    logger.info(f"Checking for existing participants in room...")
    for participant in ctx.room.remote_participants.values():
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            identity = participant.identity
            if identity.startswith("sip_"):
                customer_id = identity.replace("sip_", "")
                logger.info(f"CUSTOMER IDENTIFIED FROM EXISTING PARTICIPANT: {customer_id}")
                break
    
    if customer_id == "unknown":
        logger.warning(f"Customer ID still unknown after checking existing participants")
    
    transfer_triggered = {"value": False}
    session_ref = {"session": None}
    
    # ========================================================================
    # TRANSFER FUNCTION
    # ========================================================================
    async def execute_transfer():
        """Execute SIP transfer to human agent"""
        if transfer_triggered["value"]:
            logger.info("Transfer already in progress, skipping")
            return
            
        transfer_triggered["value"] = True
        logger.info(f"EXECUTING TRANSFER NOW")
        
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
            
            logger.info(f"Calling: sip:{agent_extension}@{fusionpbx_ip}:5060")
            logger.info(f"Using trunk: {outbound_trunk_id}")
            logger.info(f"Room: {call_id}")
            
            transfer_result = await livekit_api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=call_id,
                    sip_trunk_id=outbound_trunk_id,
                    sip_call_to=f"{agent_extension}",
                    participant_identity=f"human-agent-general",
                    participant_name=f"Human Agent",
                    participant_metadata='{"reason": "customer_request"}',
                )
            )
            
            logger.info(f"TRANSFER SUCCESS!")
            logger.info(f"Participant ID: {transfer_result.participant_id}")
            logger.info(f"Participant Identity: {transfer_result.participant_identity}")
            logger.info(f"SIP Call ID: {transfer_result.sip_call_id}")
            
            await send_to_ccm(call_id, customer_id, "Transfer initiated", "BOT")
            
        except Exception as e:
            logger.error(f"TRANSFER FAILED: {e}", exc_info=True)
            transfer_triggered["value"] = False
            await send_to_ccm(call_id, customer_id, "Transfer failed. Please try again.", "BOT")
    
    # ========================================================================
    # ROOM EVENTS
    # ========================================================================
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        logger.info(f"JOINED: {participant.identity}, Kind: {participant.kind}, SID: {participant.sid}")
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            logger.info(f"HUMAN AGENT CONNECTED - AI AGENT WILL NOW LEAVE")
            
            async def leave_room():
                try:
                    await asyncio.sleep(1)
                    logger.info("Stopping AI agent session...")
                    if session_ref["session"]:
                        await session_ref["session"].aclose()
                    logger.info("AI agent leaving room...")
                    await ctx.disconnect()
                    logger.info("AI agent left - Customer now talking to human agent")
                except Exception as e:
                    logger.error(f"Error leaving room: {e}")
            
            asyncio.create_task(leave_room())
    
    @ctx.room.on("track_subscribed")
    def on_track_subscribed(track: rtc.Track, publication: rtc.TrackPublication, participant: rtc.RemoteParticipant):
        logger.info(f"TRACK: {participant.identity} - {track.kind}")
        
        # Extract customer ID from participant identity if still unknown
        nonlocal customer_id
        if customer_id == "unknown" and participant.identity.startswith("sip_"):
            customer_id = participant.identity.replace("sip_", "")
            logger.info(f"CUSTOMER IDENTIFIED FROM TRACK: {customer_id} (from {participant.identity})")
    
    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        logger.info(f"LEFT: {participant.identity}")
    
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
    
    # ========================================================================
    # USER INPUT TRANSCRIBED EVENT - CORRECT EVENT FOR REALTIME API
    # ========================================================================
    @session.on("user_input_transcribed")
    def on_user_input_transcribed(event):
        """
        This is the CORRECT event for OpenAI Realtime API
        event.transcript contains the user's speech as text
        event.is_final indicates if this is the final transcript
        """
        transcript = event.transcript
        is_final = event.is_final
        
        logger.info(f"USER TRANSCRIPT (final={is_final}): {transcript}")
        
        # Only process final transcripts
        if not is_final:
            return
        
        # Send to CCM
        logger.info(f"Sending user transcript to CCM: {transcript}")
        asyncio.create_task(send_to_ccm(call_id, customer_id, transcript, "CONNECTOR"))
        
        # Check for transfer keywords
        transfer_keywords = ["transfer", "human", "agent", "representative", "person", "someone"]
        if any(keyword in transcript.lower() for keyword in transfer_keywords):
            logger.info(f"TRANSFER KEYWORD DETECTED: '{transcript}'")
            logger.info(f"TRIGGERING TRANSFER...")
            # Execute transfer
            asyncio.create_task(execute_transfer())
    
    # ========================================================================
    # CONVERSATION ITEM ADDED - FOR AGENT RESPONSES
    # ========================================================================
    @session.on("conversation_item_added")
    def on_conversation_item_added(event):
        """Capture agent responses"""
        item = event.item
        
        if item.role == "assistant" and hasattr(item, 'text_content') and item.text_content:
            logger.info(f"AGENT RESPONSE: {item.text_content}")
            logger.info(f"Sending agent response to CCM: {item.text_content}")
            asyncio.create_task(send_to_ccm(call_id, customer_id, item.text_content, "BOT"))
    
    # Start session
    await session.start(
        agent=Assistant(call_id, customer_id),
        room=ctx.room,
    )
    
    await ctx.connect()
    
    logger.info(f"AGENT CONNECTED TO ROOM: {call_id}")

# ============================================================================
# RUN SERVER
# ============================================================================
if __name__ == "__main__":
    cli.run_app(server)
