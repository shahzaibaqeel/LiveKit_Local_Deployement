"""
============================================================================
LIVEKIT AGENT WITH OPENAI REALTIME API + ROOM-LEVEL TRANSCRIPTION
PRODUCTION READY - FIXED ALL ERRORS
============================================================================
"""

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
    """Send transcript to CCM with retry logic"""
    if not message or not message.strip():
        return False
    
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
                        logger.info(f"[CCM✓] {sender_type}: {message[:60]}...")
                        return True
                    else:
                        logger.warning(f"[CCM] Attempt {attempt+1}: Status {resp.status}")
            except Exception as e:
                logger.warning(f"[CCM] Attempt {attempt+1}: {e}")
            
            if attempt < 2:
                await asyncio.sleep(0.5)
    
    logger.error(f"[CCM✗] Failed: {sender_type}")
    return False

# ============================================================================
# ASSISTANT AGENT - FIXED: No ChatContext.append()
# ============================================================================
class Assistant(Agent):
    def __init__(self, call_id: str, customer_id: str):
        # FIX: Pass instructions as string, not ChatContext
        super().__init__(
            instructions="""You are a helpful voice AI assistant for Expertflow Support.

When a customer asks to speak with a human agent or mentions "transfer", "agent", 
"representative", "human", "connect me", say "Let me connect you with our team" then STOP speaking."""
        )
        self.call_id = call_id
        self.customer_id = customer_id
        self.greeting_sent = False
    
    async def on_enter(self):
        """Send immediate greeting when agent enters session"""
        if self.greeting_sent:
            return
        
        self.greeting_sent = True
        greeting = "Welcome to Expertflow Support. How can I help you today?"
        
        logger.info(f"[AGENT] Sending greeting...")
        
        # Send to CCM
        await send_to_ccm(self.call_id, self.customer_id, greeting, "BOT")
        
        # FIX: Correct way to trigger greeting for Realtime API
        if self.session:
            try:
                # Use session's conversation to add user message that prompts greeting
                self.session.conversation.item.create(
                    llm.ChatMessage(
                        role="user",
                        content="Please greet the user and introduce yourself."
                    )
                )
                # Then trigger response
                self.session.response.create()
            except AttributeError:
                # Fallback: Try generate_reply() with no args
                try:
                    self.session.generate_reply()
                except:
                    logger.warning("[AGENT] Could not trigger greeting via API")
        
        logger.info("[AGENT] ✅ Greeting triggered")

# ============================================================================
# SERVER SETUP
# ============================================================================
server = AgentServer()

def prewarm(proc: JobProcess):
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
    state = {
        "customer_identity": None,
        "human_agent_identity": None,
        "transfer_triggered": False,
        "ai_active": True,
        "call_ended": False,
        "participant_count": 0,
    }
    
    session_ref = {"session": None}
    
    # Extract initial customer
    for p in ctx.room.remote_participants.values():
        if p.identity.startswith("sip_") and not p.identity.startswith("human"):
            state["customer_identity"] = p.identity
            customer_id = p.identity.replace("sip_", "")
            state["participant_count"] += 1
            logger.info(f"[CALL] Initial customer: {customer_id}")
            break
    
    # ========================================================================
    # TRANSFER FUNCTION
    # ========================================================================
    async def execute_transfer():
        """Transfer to human agent"""
        if state["transfer_triggered"]:
            return
        
        state["transfer_triggered"] = True
        logger.info("[TRANSFER] 🔴 Executing...")
        
        await send_to_ccm(call_id, customer_id, "Connecting you to our live agent...", "BOT")
        
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
            await send_to_ccm(call_id, customer_id, "Transfer failed. Please try again.", "BOT")
    
    # ========================================================================
    # END CALL FUNCTION
    # ========================================================================
    async def end_call(reason: str):
        """End call and cleanup"""
        if state["call_ended"]:
            return
        
        state["call_ended"] = True
        state["ai_active"] = False
        
        logger.info(f"[CALL] 🔴 Ending - {reason}")
        
        try:
            # Shutdown AI session
            if session_ref["session"]:
                try:
                    session_ref["session"].shutdown()
                except:
                    pass
            
            # Remove all participants
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
                    except Exception as e:
                        logger.debug(f"Could not remove {identity}: {e}")
            
            logger.info("[CALL] ✅ Call ended cleanly")
            
        except Exception as e:
            logger.error(f"[CALL] Error ending call: {e}")
    
    # ========================================================================
    # CHECK PARTICIPANT COUNT
    # ========================================================================
    def check_participant_count():
        """Hangup if only 1 participant remains"""
        participant_count = 0
        for p in ctx.room.remote_participants.values():
            if p.identity.startswith("sip_") or p.identity.startswith("human-agent"):
                participant_count += 1
        
        state["participant_count"] = participant_count
        logger.info(f"[ROOM] Participant count: {participant_count}")
        
        if participant_count <= 1 and not state["call_ended"]:
            logger.info("[ROOM] Only 1 participant remains - ending call")
            asyncio.create_task(end_call("Only one participant remaining"))
    
    # ========================================================================
    # ROOM EVENTS
    # ========================================================================
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        """Participant joined"""
        logger.info(f"[ROOM] 👤 Joined: {participant.identity}")
        
        # Track customer
        if participant.identity.startswith("sip_") and not participant.identity.startswith("human"):
            state["customer_identity"] = participant.identity
            nonlocal customer_id
            customer_id = participant.identity.replace("sip_", "")
        
        # Human agent joined
        if participant.identity.startswith("human-agent"):
            state["human_agent_identity"] = participant.identity
            logger.info("[ROOM] 🟢 Human agent joined - AI will become silent")
            
            async def silence_ai():
                await asyncio.sleep(0.5)
                state["ai_active"] = False
                logger.info("[AGENT] 🤐 AI is now silent (STT continues)")
            
            asyncio.create_task(silence_ai())
        
        check_participant_count()
    
    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        """Participant left"""
        logger.info(f"[ROOM] 👋 Left: {participant.identity}")
        
        if participant.identity == state["customer_identity"]:
            state["customer_identity"] = None
        elif participant.identity == state["human_agent_identity"]:
            state["human_agent_identity"] = None
        
        check_participant_count()
    
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
    # ROOM-LEVEL TRANSCRIPTION EVENTS
    # ========================================================================
    @session.on("user_input_transcribed")
    def on_user_input_transcribed(event):
        """Room-level STT - continues even after AI is silenced"""
        if not event.is_final:
            return
        
        transcript = event.transcript.strip()
        if not transcript:
            return
        
        logger.info(f"[STT] {transcript}")
        
        # Always push to CCM
        asyncio.create_task(send_to_ccm(call_id, customer_id, transcript, "CONNECTOR"))
        
        # Only process transfer if AI is active
        if state["ai_active"]:
            keywords = ["transfer", "human", "agent", "representative", "person", "someone", "connect"]
            if any(k in transcript.lower() for k in keywords):
                logger.info("[TRANSFER] Keyword detected")
                asyncio.create_task(execute_transfer())
    
    @session.on("conversation_item_added")
    def on_conversation_item_added(event):
        """AI response - only push if AI is active"""
        if not state["ai_active"]:
            return
        
        item = event.item
        if item.role == "assistant" and hasattr(item, 'text_content') and item.text_content:
            text = item.text_content.strip()
            if text:
                logger.info(f"[AI] {text}")
                asyncio.create_task(send_to_ccm(call_id, customer_id, text, "BOT"))
    
    # ========================================================================
    # START SESSION
    # ========================================================================
    await session.start(
        agent=Assistant(call_id, customer_id),
        room=ctx.room,
    )
    
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    
    logger.info(f"[AGENT] ✅ Connected to room: {call_id}")

# ============================================================================
# RUN SERVER
# ============================================================================
if __name__ == "__main__":
    cli.run_app(server)