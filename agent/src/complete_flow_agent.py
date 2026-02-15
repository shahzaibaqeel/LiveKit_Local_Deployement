"""
============================================================================
LIVEKIT AGENT WITH OPENAI REALTIME API + CALL TRANSFER TO HUMAN AGENT
Uses user_input_transcribed event - THE CORRECT WAY
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
    """Send transcript to CCM API - matches provided reliable reference format"""
    
    timestamp = str(int(time.time() * 1000))
    
    # 1. Base Channel Data (Common to all)
    channel_data = {
        "channelCustomerIdentifier": customer_id,  # Extracted from SIP participant
        "serviceIdentifier": "9876",           # Keep as is (per user instruction)
        "channelTypeCode": "CX_VOICE"
    }

    payload = {}

    # 2. BOT SENDER (Minimal Header)
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

    # 3. CONNECTOR / AGENT SENDER (Full Header)
    else:
        sender_obj = {}
        if sender_type == "AGENT":
            sender_obj = {
                "id": "agent_live_transfer",
                "type": "AGENT",
                "senderName": "Live Agent",
                "additionalDetail": None
            }
        else: # CONNECTOR
            sender_obj = {
                "id": "460df46c-adf9-11ed-afa1-0242ac120002",
                "type": "CONNECTOR",
                "senderName": "WEB_CONNECTOR",
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
    
    logger.info(f"📤 SENDING TO CCM [{sender_type}]: {message[:80]}...")
    logger.debug(f"📦 CCM Payload: {payload}")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://efcx4-voice.expertflow.com/ccm/message/receive",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                response_text = await resp.text()
                
                # Accept all 2xx status codes (200-299) as success
                if 200 <= resp.status < 300:
                    logger.info(f"✅ CCM SUCCESS [{sender_type}] - Status: {resp.status} - Response: {response_text}")
                    return True
                else:
                    logger.error(f"❌ CCM FAILED [{sender_type}] - Status: {resp.status} - Response: {response_text}")
                    return False
                    
    except aiohttp.ClientError as e:
        logger.error(f"❌ CCM HTTP ERROR [{sender_type}]: {type(e).__name__} - {str(e)}", exc_info=True)
        return False
    except Exception as e:
        logger.error(f"❌ CCM UNEXPECTED ERROR [{sender_type}]: {type(e).__name__} - {str(e)}", exc_info=True)
        return False


# ============================================================================
# AGENT DEFINITION
# ============================================================================
class Assistant(Agent):
    def __init__(self, call_id: str, customer_id: str) -> None:
        super().__init__()
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
    # Extract customer ID from room metadata or set default
    customer_id = "unknown"
    
    logger.info(f"🔵 NEW CALL: Room={call_id}, Initial Customer={customer_id}")
    
    transfer_triggered = {"value": False}
    
    # ========================================================================
    # CUSTOMER ID EXTRACTION FROM SIP PARTICIPANT
    # ========================================================================
    def extract_customer_id_from_participant(identity: str) -> str:
        """
        Extract customer number from SIP participant identity
        Example: 'sip_1005' -> '1005'
        Matches Jambonz's extractedUser behavior
        """
        logger.info(f"🔍 EXTRACTING CUSTOMER ID FROM: {identity}")
        
        # Handle 'sip_' prefix
        if identity.startswith("sip_"):
            extracted = identity.replace("sip_", "")
            logger.info(f"✅ Extracted (sip_ prefix): {extracted}")
            
            # FIXME: Hardcoded override for testing as per user request
            if extracted == "1005":
                logger.warning(f"⚠️ OVERRIDING CUSTOMER ID: '{extracted}' -> '99900' (For Testing)")
                return "99900"
                
            return extracted
            
        # Handle raw SIP URI if present (e.g. sip:99900@...)
        if identity.startswith("sip:"):
            # Extract between sip: and @
            try:
                extracted = identity.split(":")[1].split("@")[0]
                logger.info(f"✅ Extracted (sip: URI): {extracted}")
                return extracted
            except Exception:
                logger.warning(f"⚠️ Failed to extract from SIP URI: {identity}")
                
        logger.info(f"ℹ️ Returning raw identity: {identity}")
        return identity
    
    # ========================================================================
    # TRANSFER FUNCTION
    # ========================================================================
    async def execute_transfer():
        """Execute SIP transfer to human agent"""
        if transfer_triggered["value"]:
            logger.info("⏭️ Transfer already in progress, skipping")
            return
            
        transfer_triggered["value"] = True
        logger.info(f"🔴 EXECUTING TRANSFER NOW")
        
        await send_to_ccm(call_id, customer_id, "Connecting you to our live agent...", "BOT")

        # Update instructions to silence the bot via generic conversation item
        logger.info("silencing bot via conversation item")
        try:
             session.conversation.item.create(
                openai.realtime.RealtimeItem(
                    type="message",
                    role="user",
                    content=[{"type": "input_text", "text": "You are now a silent scribe. Do not speak. Only listen and transcribe."}]
                )
            )
        except Exception as e:
            logger.error(f"Failed to set silent mode: {e}")

        try:
            livekit_api = api.LiveKitAPI(
                url=os.getenv("LIVEKIT_URL"),
                api_key=os.getenv("LIVEKIT_API_KEY"),
                api_secret=os.getenv("LIVEKIT_API_SECRET")
            )
            
            outbound_trunk_id = "ST_W7jqvDFA2VgG"
            agent_extension = "99900"
            fusionpbx_ip = "192.168.1.17"
            
            logger.info(f"📞 Calling: sip:{agent_extension}@{fusionpbx_ip}:5060")
            logger.info(f"📞 Using trunk: {outbound_trunk_id}")
            logger.info(f"📞 Room: {call_id}")
            
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
            
            logger.info(f"✅ TRANSFER SUCCESS!")
            logger.info(f"✅ Participant ID: {transfer_result.participant_id}")
            logger.info(f"✅ Participant Identity: {transfer_result.participant_identity}")
            logger.info(f"✅ SIP Call ID: {transfer_result.sip_call_id}")
            
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
        nonlocal customer_id
        
        logger.info(f"👤 JOINED: {participant.identity}, Kind: {participant.kind}, SID: {participant.sid}")
        
        # Extract customer ID from SIP participant
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            if participant.identity.startswith("sip_"):
                customer_id = extract_customer_id_from_participant(participant.identity)
                logger.info(f"📞 CUSTOMER IDENTIFIED: {customer_id} (from {participant.identity})")
            else:
                logger.info(f"🟢 HUMAN AGENT CONNECTED TO ROOM")

    
    @ctx.room.on("track_subscribed")
    def on_track_subscribed(track: rtc.Track, publication: rtc.TrackPublication, participant: rtc.RemoteParticipant):
        nonlocal customer_id
        
        logger.info(f"🎧 TRACK: {participant.identity} - {track.kind}")
        
        # Extract customer ID from SIP participant if not already set
        if customer_id == "unknown" and participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            if participant.identity.startswith("sip_"):
                customer_id = extract_customer_id_from_participant(participant.identity)
                logger.info(f"📞 CUSTOMER IDENTIFIED FROM TRACK: {customer_id} (from {participant.identity})")
    
    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        logger.info(f"👋 LEFT: {participant.identity}")
    
    # ========================================================================
    # EXTRACT CUSTOMER ID FROM EXISTING PARTICIPANTS (TIMING FIX)
    # ========================================================================
    # The SIP participant often joins BEFORE this event handler is registered
    # So we need to check existing participants in the room
    logger.info(f"🔍 Checking for existing participants in room...")
    
    for participant_sid, participant in ctx.room.remote_participants.items():
        logger.info(f"👥 Found existing participant: {participant.identity}, Kind: {participant.kind}, SID: {participant_sid}")
        
        # Extract customer ID from existing SIP participant
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            if participant.identity.startswith("sip_"):
                customer_id = extract_customer_id_from_participant(participant.identity)
                logger.info(f"📞 CUSTOMER IDENTIFIED FROM EXISTING PARTICIPANT: {customer_id} (from {participant.identity})")
                break  # Found the customer
    
    if customer_id == "unknown":
        logger.warning(f"⚠️ Customer ID still unknown after checking existing participants")
    
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
    # TRANSCRIPTION TRACKING - PREVENT DUPLICATES
    # ========================================================================
    sent_transcripts = set()
    
    # ========================================================================
    # USER INPUT TRANSCRIBED EVENT - CAPTURES USER SPEECH
    # ========================================================================
    @session.on("user_input_transcribed")
    def on_user_input_transcribed(event):
        """
        Captures user speech transcriptions from OpenAI Realtime API
        event.transcript: The transcribed text
        event.is_final: Whether this is the final version
        """
        transcript = event.transcript
        is_final = event.is_final
        
        logger.info(f"👤 USER TRANSCRIPT (final={is_final}): {transcript}")
        
        # Only process final transcripts to avoid duplicates
        if not is_final:
            return
        
        # Skip empty transcripts
        if not transcript or transcript.strip() == "":
            logger.warning("⚠️ Empty user transcript received, skipping")
            return
            
        # Send to CCM with error handling
        try:
            asyncio.create_task(send_to_ccm(call_id, customer_id, transcript, "CONNECTOR"))
            logger.info(f"✅ User transcript queued for CCM: '{transcript[:50]}...'")
        except Exception as e:
            logger.error(f"❌ Failed to queue user transcript to CCM: {e}")
        
        # Check for transfer keywords
        transfer_keywords = ["transfer", "human", "agent", "representative", "person", "someone"]
        if any(keyword in transcript.lower() for keyword in transfer_keywords):
            logger.info(f"🔍 TRANSFER KEYWORD DETECTED: '{transcript}'")
            logger.info(f"🚀 TRIGGERING TRANSFER...")
            asyncio.create_task(execute_transfer())
    
    # ========================================================================
    # SPEECH CREATED EVENT - CAPTURES AGENT AUDIO RESPONSES
    # ========================================================================
    @session.on("speech_created")
    def on_speech_created(event):
        """
        Captures when agent speech is created (TTS audio being generated)
        This is the PRIMARY way to capture agent responses in real-time
        """
        if hasattr(event, 'text') and event.text:
            agent_text = event.text
            
            # Deduplicate using hash
            text_hash = hash(agent_text)
            if text_hash in sent_transcripts:
                logger.debug(f"⏭️ Skipping duplicate agent response: '{agent_text[:30]}...'")
                return
            
            sent_transcripts.add(text_hash)
            logger.info(f"🤖 AGENT SPEECH CREATED: {agent_text}")
            
            try:
                asyncio.create_task(send_to_ccm(call_id, customer_id, agent_text, "BOT"))
                logger.info(f"✅ Agent response queued for CCM: '{agent_text[:50]}...'")
            except Exception as e:
                logger.error(f"❌ Failed to queue agent response to CCM: {e}")
    
    # ========================================================================
    # AGENT STARTED SPEAKING - ADDITIONAL CAPTURE POINT
    # ========================================================================
    @session.on("agent_started_speaking")
    def on_agent_started_speaking(event):
        """
        Backup handler when agent starts speaking
        Provides additional capture point for agent responses
        """
        logger.info(f"🎙️ AGENT STARTED SPEAKING")
        # This event typically doesn't have text, but we log it for debugging
    
    # ========================================================================
    # CONVERSATION ITEM ADDED - BACKUP FOR TEXT-BASED AGENT RESPONSES
    # ========================================================================
    @session.on("conversation_item_added")
    def on_conversation_item_added(event):
        """
        Backup handler for agent responses (text-based)
        This captures responses that might not go through agent_speech
        """
        item = event.item
        
        if item.role == "assistant":
            # Try to extract text content
            agent_text = None
            
            if hasattr(item, 'text_content') and item.text_content:
                agent_text = item.text_content
            elif hasattr(item, 'content') and item.content:
                # Handle different content formats
                if isinstance(item.content, str):
                    agent_text = item.content
                elif isinstance(item.content, list) and len(item.content) > 0:
                    # Extract text from content array
                    for content_item in item.content:
                        if hasattr(content_item, 'text') and content_item.text:
                            agent_text = content_item.text
                            break
            
            if agent_text:
                # Deduplicate
                text_hash = hash(agent_text)
                if text_hash in sent_transcripts:
                    logger.debug(f"⏭️ Skipping duplicate agent item: '{agent_text[:30]}...'")
                    return
                
                sent_transcripts.add(text_hash)
                logger.info(f"🤖 AGENT ITEM: {agent_text}")
                
                try:
                    asyncio.create_task(send_to_ccm(call_id, customer_id, agent_text, "BOT"))
                    logger.info(f"✅ Agent item queued for CCM: '{agent_text[:50]}...'")
                except Exception as e:
                    logger.error(f"❌ Failed to queue agent item to CCM: {e}")
    
    # Start session
    await session.start(
        agent=Assistant(call_id, customer_id),
        room=ctx.room,
    )
    
    await ctx.connect()
    
    logger.info(f"✅ AGENT CONNECTED TO ROOM: {call_id}")
    
    # DEBUG: Inspect session object
    logger.info(f"🧐 Session Type: {type(session)}")
    logger.info(f"🧐 Session Dir: {dir(session)}")
    
    # ========================================================================
    # SET SYSTEM INSTRUCTIONS
    # ========================================================================
    # Add system instructions as the first conversation item
    try:
        session.conversation.item.create(
            openai.realtime.RealtimeItem(
                type="message",
                role="system",
                content=[{
                    "type": "input_text",
                    "text": """You are a helpful voice AI assistant.

When a customer asks to speak with a human agent or mentions 'transfer', 'agent', 
'representative', 'human', 'connect me', say 'Let me connect you with our team' then STOP speaking."""
                }]
            )
        )
        logger.info("✅ System instructions set")
    except Exception as e:
        logger.error(f"❌ Failed to set system instructions: {e}")
    
    # ========================================================================
    # FORCE WELCOME MESSAGE
    # ========================================================================
    # Wait a moment for connection to stabilize
    await asyncio.sleep(1)
    
    welcome_text = "Welcome to ExpertFlow. How can I assist you today?"
    logger.info(f"📢 TRIGGERING WELCOME MESSAGE: '{welcome_text}'")
    
    # Use conversation item to trigger response since session.response might not be available
    # We send a "user" message command which the model should reply to
    try:
        session.conversation.item.create(
            openai.realtime.RealtimeItem(
                type="message", 
                role="user", 
                content=[{"type": "input_text", "text": f"Say exactly: '{welcome_text}'"}]
            )
        )
        # Attempt to trigger generation if possible, otherwise rely on VAD/auto-turn
        if hasattr(session, 'response') and hasattr(session.response, 'create'):
             await session.response.create()
    except Exception as e:
        logger.error(f"❌ Failed to trigger welcome message: {e}")

# ============================================================================
# RUN SERVER
# ============================================================================
if __name__ == "__main__":
    cli.run_app(server)