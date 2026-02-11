"""
===================================================================================
LIVEKIT AGENT WITH OPENAI REALTIME API + CALL TRANSFER + FULL TRANSCRIPTION
Based on Jambonz working implementation - PRODUCTION READY
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
    """Send transcript to CCM API - Matches Jambonz implementation"""
    logger.info(f"[CCM] Sending {sender_type} message: {message[:50]}...")
    
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
                if resp.status == 200:
                    logger.info(f"[CCM] ✅ Sent successfully: {sender_type}")
                else:
                    logger.error(f"[CCM] ❌ Failed with status: {resp.status}")
    except Exception as e:
        logger.error(f"[CCM] ❌ Error: {e}")

# ============================================================================
# AGENT DEFINITION WITH GREETING
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
        """Called when agent enters session - Send immediate greeting"""
        if self.greeting_sent:
            return
        
        self.greeting_sent = True
        welcome_msg = "Welcome to Expertflow Support, let me know how I can help you?"
        
        logger.info(f"[AGENT] on_enter() called - Sending greeting")
        
        # Send to CCM first
        await send_to_ccm(self.call_id, self.customer_id, welcome_msg, "BOT")
        
        # Trigger agent to speak using generateReply()
        self.session.generate_reply(
            instructions=f"Say exactly: '{welcome_msg}'"
        )
        
        logger.info(f"[AGENT] ✅ Greeting initiated via generate_reply()")

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
    
    logger.info(f"[CALL] 🔵 NEW CALL: Room={call_id}, Customer={customer_id}")
    
    # Track participants
    customer_identity = {"value": None}
    human_agent_identity = {"value": None}
    transfer_triggered = {"value": False}
    session_ref = {"session": None}
    ai_agent_active = {"value": True}
    
    # ========================================================================
    # DISCONNECT ENTIRE CALL
    # ========================================================================
    async def disconnect_call(reason: str):
        """Disconnect all participants and end the call"""
        logger.info(f"[CALL] 🔴 Disconnecting - Reason: {reason}")
        
        try:
            livekit_api = api.LiveKitAPI(
                url=os.getenv("LIVEKIT_URL"),
                api_key=os.getenv("LIVEKIT_API_KEY"),
                api_secret=os.getenv("LIVEKIT_API_SECRET")
            )
            
            # Remove all participants
            participants_to_remove = []
            if customer_identity["value"]:
                participants_to_remove.append(customer_identity["value"])
            if human_agent_identity["value"]:
                participants_to_remove.append(human_agent_identity["value"])
            
            for identity in participants_to_remove:
                try:
                    await livekit_api.room.remove_participant(
                        room=call_id,
                        identity=identity
                    )
                    logger.info(f"[CALL] ✅ Removed participant: {identity}")
                except Exception as e:
                    logger.error(f"[CALL] ❌ Error removing {identity}: {e}")
            
            # Close session and disconnect agent
            if session_ref["session"]:
                await session_ref["session"].aclose()
            await ctx.disconnect()
            
            logger.info(f"[CALL] ✅ Call fully disconnected")
            
        except Exception as e:
            logger.error(f"[CALL] ❌ Error in disconnect_call: {e}", exc_info=True)
    
    # ========================================================================
    # TRANSFER FUNCTION
    # ========================================================================
    async def execute_transfer():
        """Execute SIP transfer to human agent"""
        if transfer_triggered["value"]:
            logger.info("[TRANSFER] ⏭️ Transfer already in progress")
            return
            
        transfer_triggered["value"] = True
        logger.info(f"[TRANSFER] 🔴 EXECUTING TRANSFER NOW")
        
        await send_to_ccm(call_id, customer_id, "Connecting you to our live agent...", "BOT")
        
        try:
            livekit_api = api.LiveKitAPI(
                url=os.getenv("LIVEKIT_URL"),
                api_key=os.getenv("LIVEKIT_API_KEY"),
                api_secret=os.getenv("LIVEKIT_API_SECRET")
            )
            
            outbound_trunk_id = "ST_W7jqvDFA2VgG"
            agent_extension = "99900"
            
            logger.info(f"[TRANSFER] 📞 Calling: {agent_extension}")
            
            transfer_result = await livekit_api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=call_id,
                    sip_trunk_id=outbound_trunk_id,
                    sip_call_to=f"{agent_extension}",
                    participant_identity=f"human-agent-{customer_id}",
                    participant_name=f"Human Agent",
                    participant_metadata='{"reason": "customer_request"}',
                )
            )
            
            logger.info(f"[TRANSFER] ✅ SUCCESS!")
            logger.info(f"[TRANSFER] SIP Call ID: {transfer_result.sip_call_id}")
            
            await send_to_ccm(call_id, customer_id, "Transfer initiated", "BOT")
            
        except Exception as e:
            logger.error(f"[TRANSFER] ❌ FAILED: {e}", exc_info=True)
            transfer_triggered["value"] = False
            await send_to_ccm(call_id, customer_id, "Transfer failed. Please try again.", "BOT")
    
    # ========================================================================
    # ROOM EVENTS
    # ========================================================================
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        logger.info(f"[ROOM] 👤 JOINED: {participant.identity}, Kind: {participant.kind}")
        
        # Track customer
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP and participant.identity.startswith("sip_"):
            customer_identity["value"] = participant.identity
            logger.info(f"[ROOM] 📞 Customer tracked: {customer_identity['value']}")
        
        # Human agent connected
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP and participant.identity.startswith("human-agent"):
            human_agent_identity["value"] = participant.identity
            logger.info(f"[ROOM] 🟢 HUMAN AGENT CONNECTED")
            logger.info(f"[ROOM] Tracked human agent: {human_agent_identity['value']}")
            
            # AI agent leaves
            async def ai_agent_leave():
                try:
                    await asyncio.sleep(1)
                    ai_agent_active["value"] = False
                    logger.info("[AGENT] 🤖 AI agent stopping...")
                    if session_ref["session"]:
                        await session_ref["session"].aclose()
                    await ctx.disconnect()
                    logger.info("[AGENT] ✅ AI agent left - Customer now with human agent")
                except Exception as e:
                    logger.error(f"[AGENT] ❌ Error leaving: {e}")
            
            asyncio.create_task(ai_agent_leave())
    
    @ctx.room.on("track_subscribed")
    def on_track_subscribed(track: rtc.Track, publication: rtc.TrackPublication, participant: rtc.RemoteParticipant):
        logger.info(f"[ROOM] 🎧 TRACK: {participant.identity} - {track.kind}")
        
        # Extract customer ID if still unknown
        nonlocal customer_id
        if customer_id == "unknown" and participant.identity.startswith("sip_"):
            customer_id = participant.identity.replace("sip_", "")
            logger.info(f"[ROOM] 📞 Customer ID identified: {customer_id}")
    
    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        logger.info(f"[ROOM] 👋 LEFT: {participant.identity}")
        
        # Customer disconnected - end call
        if participant.identity == customer_identity["value"]:
            logger.info(f"[ROOM] 🔴 Customer disconnected - Ending call")
            asyncio.create_task(disconnect_call("Customer disconnected"))
        
        # Human agent disconnected - end call
        elif participant.identity == human_agent_identity["value"]:
            logger.info(f"[ROOM] 🔴 Human agent disconnected - Ending call")
            asyncio.create_task(disconnect_call("Human agent disconnected"))
    
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
    # USER INPUT TRANSCRIBED - CUSTOMER SPEAKS
    # ========================================================================
    @session.on("user_input_transcribed")
    def on_user_input_transcribed(event):
        """Customer speech transcription"""
        transcript = event.transcript
        is_final = event.is_final
        
        logger.info(f"[USER] 👤 Transcript (final={is_final}): {transcript}")
        
        if not is_final or not ai_agent_active["value"]:
            return
        
        # Send to CCM
        asyncio.create_task(send_to_ccm(call_id, customer_id, transcript, "CONNECTOR"))
        
        # Check for transfer keywords
        transfer_keywords = ["transfer", "human", "agent", "representative", "person", "someone"]
        if any(keyword in transcript.lower() for keyword in transfer_keywords):
            logger.info(f"[TRANSFER] 🔍 Keyword detected: '{transcript}'")
            asyncio.create_task(execute_transfer())
    
    # ========================================================================
    # CONVERSATION ITEM ADDED - AI AGENT SPEAKS
    # ========================================================================
    @session.on("conversation_item_added")
    def on_conversation_item_added(event):
        """AI agent response"""
        item = event.item
        
        if item.role == "assistant" and hasattr(item, 'text_content') and item.text_content:
            if not ai_agent_active["value"]:
                return
            
            logger.info(f"[AGENT] 🤖 Response: {item.text_content}")
            asyncio.create_task(send_to_ccm(call_id, customer_id, item.text_content, "BOT"))
    
    # ========================================================================
    # START SESSION
    # ========================================================================
    await session.start(
        agent=Assistant(call_id, customer_id),
        room=ctx.room,
    )
    
    await ctx.connect()
    
    logger.info(f"[AGENT] ✅ CONNECTED TO ROOM: {call_id}")
    logger.info(f"[AGENT] Greeting will be sent via on_enter()")

# ============================================================================
# RUN SERVER
# ============================================================================
if __name__ == "__main__":
    cli.run_app(server)
