"""
============================================================================
LIVEKIT AGENT WITH OPENAI REALTIME API + CALL TRANSFER + CCM TRANSCRIPTION
PRODUCTION READY - TESTED AND WORKING
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
    AutoSubscribe,
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
# CCM API HELPER - FIXED VERSION
# ============================================================================
async def send_to_ccm(call_id: str, customer_id: str, message: str, sender_type: str):
    """Send transcript to CCM API"""
    
    if not message or not message.strip():
        logger.warning(f"[CCM] Empty message, skipping")
        return False
    
    timestamp = str(int(time.time() * 1000))
    
    channel_data = {
        "channelCustomerIdentifier": customer_id,
        "serviceIdentifier": "9876",
        "channelTypeCode": "CX_VOICE"
    }

    # Build payload based on sender type
    if sender_type == "BOT":
        payload = {
            "id": call_id,
            "header": {
                "channelData": channel_data,
                "sender": {
                    "id": "6540b0fc90b3913194d45525",
                    "type": "BOT",
                    "senderName": "Voice Bot"
                },
                "timestamp": timestamp
            },
            "body": {
                "type": "PLAIN",
                "markdownText": message
            }
        }
    else:
        sender_obj = {
            "id": "agent_live_transfer" if sender_type == "AGENT" else "460df46c-adf9-11ed-afa1-0242ac120002",
            "type": sender_type,
            "senderName": "Live Agent" if sender_type == "AGENT" else "WEB_CONNECTOR",
            "additionalDetail": None
        }
        
        payload = {
            "id": call_id,
            "header": {
                "channelData": channel_data,
                "sender": sender_obj,
                "language": {},
                "timestamp": timestamp,
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
    
    logger.info(f"📤 CCM [{sender_type}]: {message[:80]}...")
    
    # Send with proper error handling
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                "https://efcx4-voice.expertflow.com/ccm/message/receive",
                json=payload,
                headers={"Content-Type": "application/json"}
            ) as resp:
                response_text = await resp.text()
                
                if 200 <= resp.status < 300:
                    logger.info(f"✅ CCM SUCCESS [{sender_type}] - Status: {resp.status}")
                    return True
                else:
                    logger.error(f"❌ CCM FAILED [{sender_type}] - Status: {resp.status} - {response_text}")
                    return False
                    
    except asyncio.TimeoutError:
        logger.error(f"❌ CCM TIMEOUT [{sender_type}]")
        return False
    except Exception as e:
        logger.error(f"❌ CCM ERROR [{sender_type}]: {e}")
        return False


# ============================================================================
# AGENT DEFINITION
# ============================================================================
class Assistant(Agent):
    def __init__(self, call_id: str, customer_id: str) -> None:
        super().__init__(
            instructions="""You are a helpful voice AI assistant.

When a customer asks to speak with a human agent or mentions "transfer", "agent", 
"representative", "human", "connect me", say "Let me connect you with our team" then STOP speaking.""",
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
    customer_id = "unknown"
    
    logger.info(f"🔵 NEW CALL: Room={call_id}")
    
    # State
    state = {
        "customer_id": "unknown",
        "transfer_triggered": False,
        "sent_transcripts": set(),
    }
    
    # ========================================================================
    # CUSTOMER ID EXTRACTION
    # ========================================================================
    def extract_customer_id(identity: str) -> str:
        """Extract customer ID from participant identity"""
        logger.info(f"🔍 Extracting from: {identity}")
        
        if identity.startswith("sip_"):
            extracted = identity.replace("sip_", "")
            logger.info(f"✅ Extracted: {extracted}")
            
            # Testing override
            if extracted == "1005":
                logger.warning(f"⚠️ Override: 1005 → 99900")
                return "99900"
            
            return extracted
        
        if identity.startswith("sip:"):
            try:
                extracted = identity.split(":")[1].split("@")[0]
                logger.info(f"✅ Extracted from URI: {extracted}")
                return extracted
            except:
                pass
        
        return identity
    
    # ========================================================================
    # TRANSFER FUNCTION
    # ========================================================================
    async def execute_transfer():
        """Transfer to human agent"""
        if state["transfer_triggered"]:
            return
        
        state["transfer_triggered"] = True
        logger.info("🔴 EXECUTING TRANSFER")
        
        await send_to_ccm(call_id, state["customer_id"], "Connecting you to our live agent...", "BOT")
        
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
                    participant_identity="human-agent-general",
                    participant_name="Human Agent",
                    participant_metadata='{"reason": "customer_request"}',
                )
            )
            
            logger.info(f"✅ TRANSFER SUCCESS: {result.sip_call_id}")
            await send_to_ccm(call_id, state["customer_id"], "Transfer initiated", "BOT")
            
        except Exception as e:
            logger.error(f"❌ TRANSFER FAILED: {e}")
            state["transfer_triggered"] = False
            await send_to_ccm(call_id, state["customer_id"], "Transfer failed", "BOT")
    
    # ========================================================================
    # ROOM EVENTS
    # ========================================================================
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        logger.info(f"👤 JOINED: {participant.identity}")
        
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            if participant.identity.startswith("sip_"):
                state["customer_id"] = extract_customer_id(participant.identity)
                logger.info(f"📞 CUSTOMER: {state['customer_id']}")
            else:
                logger.info("🟢 HUMAN AGENT JOINED")
    
    @ctx.room.on("track_subscribed")
    def on_track_subscribed(track: rtc.Track, publication: rtc.TrackPublication, participant: rtc.RemoteParticipant):
        logger.info(f"🎧 TRACK: {participant.identity}")
        
        if state["customer_id"] == "unknown" and participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            if participant.identity.startswith("sip_"):
                state["customer_id"] = extract_customer_id(participant.identity)
                logger.info(f"📞 CUSTOMER (from track): {state['customer_id']}")
    
    # ========================================================================
    # CHECK EXISTING PARTICIPANTS
    # ========================================================================
    logger.info("🔍 Checking existing participants...")
    for sid, p in ctx.room.remote_participants.items():
        logger.info(f"👥 Found: {p.identity}")
        
        if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP and p.identity.startswith("sip_"):
            state["customer_id"] = extract_customer_id(p.identity)
            logger.info(f"📞 CUSTOMER (existing): {state['customer_id']}")
            break
    
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
    # SESSION EVENT HANDLERS - CRITICAL: Must be defined BEFORE session.start()
    # ========================================================================
    
    @session.on("user_input_transcribed")
    def on_user_transcribed(event):
        """Customer speaks"""
        if not event.is_final:
            return
        
        transcript = event.transcript.strip()
        if not transcript:
            return
        
        logger.info(f"👤 CUSTOMER: {transcript}")
        
        # CRITICAL FIX: Use asyncio.ensure_future() instead of create_task()
        # This works in synchronous event handlers
        asyncio.ensure_future(
            send_to_ccm(call_id, state["customer_id"], transcript, "CONNECTOR")
        )
        
        # Check transfer keywords
        keywords = ["transfer", "human", "agent", "representative", "person", "someone"]
        if any(k in transcript.lower() for k in keywords):
            logger.info(f"🔍 TRANSFER KEYWORD DETECTED")
            asyncio.ensure_future(execute_transfer())
    
    @session.on("agent_speech")
    def on_agent_speech(event):
        """AI speaks - Primary handler"""
        if hasattr(event, 'text') and event.text:
            text = event.text.strip()
            if not text:
                return
            
            # Deduplicate
            text_hash = hash(text)
            if text_hash in state["sent_transcripts"]:
                return
            
            state["sent_transcripts"].add(text_hash)
            logger.info(f"🤖 AI: {text}")
            
            asyncio.ensure_future(
                send_to_ccm(call_id, state["customer_id"], text, "BOT")
            )
    
    @session.on("conversation_item_added")
    def on_conversation_item(event):
        """AI speaks - Backup handler"""
        item = event.item
        
        if item.role != "assistant":
            return
        
        # Extract text
        text = None
        if hasattr(item, 'text_content') and item.text_content:
            text = item.text_content.strip()
        elif hasattr(item, 'content'):
            if isinstance(item.content, str):
                text = item.content.strip()
            elif isinstance(item.content, list):
                for c in item.content:
                    if hasattr(c, 'text') and c.text:
                        text = c.text.strip()
                        break
        
        if not text:
            return
        
        # Deduplicate
        text_hash = hash(text)
        if text_hash in state["sent_transcripts"]:
            return
        
        state["sent_transcripts"].add(text_hash)
        logger.info(f"🤖 AI (backup): {text}")
        
        asyncio.ensure_future(
            send_to_ccm(call_id, state["customer_id"], text, "BOT")
        )
    
    # ========================================================================
    # START SESSION
    # ========================================================================
    logger.info("🚀 Starting session...")
    
    await session.start(
        agent=Assistant(call_id, state["customer_id"]),
        room=ctx.room,
    )
    
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    
    logger.info(f"✅ CONNECTED TO ROOM: {call_id}")

# ============================================================================
# RUN SERVER
# ============================================================================
if __name__ == "__main__":
    cli.run_app(server)