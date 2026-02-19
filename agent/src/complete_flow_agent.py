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
    stt,
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
async def send_to_ccm(call_id: str, customer_id: str, message: str, sender_type: str, session: aiohttp.ClientSession = None):
    """Send transcript to CCM API - matches provided reliable reference format"""
    
    timestamp = str(int(time.time() * 1000))
    
    # 1. Base Channel Data (Common to all)
    channel_data = {
        "channelCustomerIdentifier": customer_id,  # Map to 99900 via the identification logic
        "serviceIdentifier": "1122",           # Keep as is (per user instruction)
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
    
    logger.info(f"📤 SENDING TO CCM [{sender_type}]: {message[:80]}...")

    if session:
        return await _post_to_ccm(session, payload, sender_type)
    else:
        async with aiohttp.ClientSession() as new_session:
            return await _post_to_ccm(new_session, payload, sender_type)

async def _post_to_ccm(session: aiohttp.ClientSession, payload: dict, sender_type: str):
    url = "https://efcx-dev2.expertflow.com/ccm/message/receive"
    try:
        async with session.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            response_text = await resp.text()
            if 200 <= resp.status < 300:
                logger.info(f"✅ CCM SUCCESS [{sender_type}] - Status: {resp.status}")
                return True
            else:
                logger.error(f"❌ CCM FAILED [{sender_type}] - Status: {resp.status} - Response: {response_text}")
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
    
    # ========================================================================
    # INITIALIZE PERSISTENT HTTP SESSION
    # ========================================================================
    if "http_session" not in ctx.proc.userdata:
        ctx.proc.userdata["http_session"] = aiohttp.ClientSession()
        logger.info("� Persistent HTTP session created")

    # ========================================================================
    # INITIALIZE SESSION & STATE EARLY (Prevents NameError in handlers)
    # ========================================================================
    bot_muted = False
    sent_transcripts = set()
    transfer_triggered = {"value": False}
    
    # Initialize session first so handlers can reference it
    session = AgentSession(
        llm=openai.realtime.RealtimeModel(
            model="gpt-4o-realtime-preview-2024-12-17",
            voice="alloy",
            temperature=0.6,
            modalities=['text', 'audio'],
        ),
        vad=ctx.proc.userdata["vad"],
    )
    assistant = Assistant(call_id, customer_id)

    # Diagnostic check for STT attributes
    logger.info(f"🔍 STT Attributes: {dir(stt)}")

    
    def extract_customer_id_from_participant(participant: rtc.RemoteParticipant) -> str:
        """
        Extract customer number from SIP participant.
        Logs all metadata for diagnostic purposes.
        """
        identity = participant.identity
        name = participant.name
        metadata = participant.metadata
        
        logger.info(f"🔍 [DIAGNOSTIC] Participant Identity: '{identity}'")
        logger.info(f"🔍 [DIAGNOSTIC] Participant Name: '{name}'")
        logger.info(f"🔍 [DIAGNOSTIC] Participant Metadata: '{metadata}'")
        logger.info(f"🔍 [DIAGNOSTIC] Room Name: '{call_id}'")
        
        extracted = identity
        
        # Handle 'sip_' prefix
        if identity.startswith("sip_"):
            extracted = identity.replace("sip_", "")
        # Handle raw SIP URI
        elif identity.startswith("sip:"):
            try:
                extracted = identity.split(":")[1].split("@")[0]
            except Exception:
                pass

        # 2. Try to recover from metadata
        if metadata:
            import json
            try:
                data = json.loads(metadata)
                for key in ["customer_id", "phoneNumber", "number", "from"]:
                    if data.get(key):
                        logger.info(f"✅ RECOVERED ID FROM METADATA '{key}': {data[key]}")
                        return str(data[key])
            except Exception:
                pass

        # 3. IF NO SPECIFIC ID FOUND AND IT IS GENERIC -> FORCE TO 99900
        # This matches the user's specific environment requirement.
        if extracted.lower() in ["freeswitch", "unknown", "agent", ""]:
            logger.info(f"📍 FORCING GENERIC IDENTITY '{extracted}' -> '99900' (Target ID)")
            return "99900"

        # Hardcoded override for testing if still necessary
        if extracted == "1005":
            logger.warning(f"⚠️ OVERRIDING CUSTOMER ID: '1005' -> '99900' (Testing)")
            return "99900"
            
        logger.info(f"✅ Final ID: {extracted}")
        return extracted
    
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
        
        # IMMEDIATELY MUTE BOT ON TRANSFER TRIGGER
        nonlocal bot_muted
        logger.info("🛑 TRANSFER TRIGGERED - SILENCING BOT IMMEDIATELY")
        bot_muted = True
        try:
            for track_sid, pub in ctx.room.local_participant.track_publications.items():
                if pub.track and pub.track.kind == rtc.TrackKind.KIND_AUDIO:
                    logger.info(f"🔇 Hardware-muting bot audio track (Transfer): {track_sid}")
                    pub.track.enabled = False
            
            # Interupt any ongoing speech
            session.push_audio(None) # Interupt
            
            inner_session = getattr(session, '_session', session)
            if hasattr(inner_session, 'update_session'):
                 logger.info("🛑 Disabling turn detection on session (Transfer)")
                 asyncio.create_task(inner_session.update_session(turn_detection=None))
        except Exception as e:
            logger.error(f"❌ Failed to hardware-mute bot tracks during transfer: {e}")

        await send_to_ccm(call_id, customer_id, "Connecting you to our live agent...", "BOT", ctx.proc.userdata["http_session"])
        
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
            
            await send_to_ccm(call_id, customer_id, "Transfer initiated", "BOT", ctx.proc.userdata["http_session"])
            
        except Exception as e:
            logger.error(f"❌ TRANSFER FAILED: {e}", exc_info=True)
            transfer_triggered["value"] = False
            await send_to_ccm(call_id, customer_id, "Transfer failed. Please try again.", "BOT", ctx.proc.userdata["http_session"])
    
    # ========================================================================
    # TRANSCRIPTION HANDLERS
    # ========================================================================

    # ========================================================================
    # ROOM EVENTS
    # ========================================================================
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        nonlocal customer_id, bot_muted
        
        logger.info(f"👤 JOINED: {participant.identity}, Kind: {participant.kind}, SID: {participant.sid}")
        
        # Extract customer ID from SIP participant
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            if participant.identity != "human-agent-general":
                customer_id = extract_customer_id_from_participant(participant)
                logger.info(f"📞 CUSTOMER IDENTIFIED: {customer_id}")
            else:
                logger.info(f"🟢 HUMAN AGENT CONNECTED TO ROOM: {participant.identity}")
                
                # STOP BOT FROM RESPONDING WHEN AGENT JOINS
                logger.info("🛑 HUMAN AGENT DETECTED - SILENCING BOT (TRACK MUTING + FLAG)")
                bot_muted = True
                
                try:
                    for track_sid, pub in ctx.room.local_participant.track_publications.items():
                        if pub.track and pub.track.kind == rtc.TrackKind.KIND_AUDIO:
                            logger.info(f"🔇 Hardware-muting bot audio track: {track_sid}")
                            pub.track.enabled = False
                    
                    # Also tell the LLM to stop generating responses (Disable VAD)
                    inner_session = getattr(session, '_session', session)
                    if hasattr(inner_session, 'update_session'):
                         logger.info("🛑 Disabling turn detection on session")
                         asyncio.create_task(inner_session.update_session(turn_detection=None))
                except Exception as e:
                    logger.error(f"❌ Failed to hardware-mute bot tracks: {e}")


    @ctx.room.on("track_subscribed")
    def on_track_subscribed(track: rtc.Track, publication: rtc.TrackPublication, participant: rtc.RemoteParticipant):
        nonlocal customer_id
        
        logger.info(f"🎧 TRACK: {participant.identity} - {track.kind}")
        
        # 1. Customer Identification (Existing Logic)
        if customer_id == "unknown" and participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            if participant.identity != "human-agent-general":
                customer_id = extract_customer_id_from_participant(participant)
                logger.info(f"📞 CUSTOMER IDENTIFIED FROM TRACK: {customer_id}")
        
        # 2. Human Agent Transcription
        if participant.identity == "human-agent-general" or participant.name == "Human Agent":
            logger.info(f"🎙️ SUBSCRIBED TO HUMAN AGENT AUDIO: {participant.identity}")
            
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                async def transcribe_agent_audio(audio_track):
                    logger.info("🚀 STARTING HUMAN AGENT TRANSCRIPTION STREAM")
                    await asyncio.sleep(0.5) # Wait for track stabilization
                    audio_stream = rtc.AudioStream(audio_track)
                    stt_instance = openai.STT() # Back to default, might be more stable than explicit whisper-1 in some versions
                    
                    stt_stream = stt_instance.stream()
                    
                    async def audio_feeder():
                        frames_pushed = 0
                        try:
                            async for chunk in audio_stream:
                                # Fix: AudioStream yields AudioFrameEvent, we need the frame
                                frame = getattr(chunk, 'frame', chunk)
                                if frame:
                                    stt_stream.push_frame(frame)
                                    frames_pushed += 1
                                    if frames_pushed % 100 == 0:
                                        logger.debug(f"📤 Pushed {frames_pushed} agent audio frames")
                            stt_stream.end_input()
                            logger.info(f"✅ Finished pushing {frames_pushed} frames for agent {participant.identity}")
                        except Exception as e:
                            logger.error(f"❌ Agent audio feeder error: {e}")
                        
                    asyncio.create_task(audio_feeder())
                    
                    async for event in stt_stream:
                        # Defensive check for event type
                        is_final = False
                        is_error = False
                        
                        # Use getattr to safely check for ERROR member
                        if hasattr(stt, 'SpeechEventType'):
                            is_final = (event.type == stt.SpeechEventType.FINAL_TRANSCRIPT)
                            # Safe check for ERROR attribute which might be missing in some versions
                            error_type = getattr(stt.SpeechEventType, 'ERROR', None)
                            is_error = (event.type == error_type) if error_type else (event.type == 3) # Fallback to common enum value
                        elif hasattr(stt, 'STTEventType'):
                            is_final = (event.type == stt.STTEventType.FINAL_TRANSCRIPT)
                            error_type = getattr(stt.STTEventType, 'ERROR', None)
                            is_error = (event.type == error_type) if error_type else False
                        
                        if is_final:
                             text = event.alternatives[0].text
                             if text and text.strip():
                                 logger.info(f"👨‍💼 AGENT TRANSCRIPT: '{text}' (Confidence: {event.alternatives[0].confidence})")
                                 asyncio.create_task(send_to_ccm(call_id, customer_id, text, "AGENT", ctx.proc.userdata["http_session"]))
                        elif is_error:
                             logger.error(f"❌ Agent STT Error: {getattr(event, 'error', 'Unknown Error')}")
                             # If we get error 1006, the stream is dead, break and let it possibly restart if handler is recalled
                             if "1006" in str(getattr(event, 'error', '')):
                                 break
                
                # Run transcription for this track
                asyncio.create_task(transcribe_agent_audio(track))

    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        logger.info(f"👋 LEFT: {participant.identity}")
    
    # ========================================================================
    # EXTRACT CUSTOMER ID FROM EXISTING PARTICIPANTS (TIMING FIX)
    # ========================================================================
    logger.info(f"🔍 Checking for existing participants in room...")
    
    for participant_sid, participant in ctx.room.remote_participants.items():
        logger.info(f"👥 Found existing participant: {participant.identity}, Kind: {participant.kind}, SID: {participant_sid}")
        
        # Extract customer ID from existing SIP participant
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            if participant.identity != "human-agent-general":
                customer_id = extract_customer_id_from_participant(participant)
                logger.info(f"📞 CUSTOMER IDENTIFIED FROM EXISTING PARTICIPANT: {customer_id}")
                break
    
    if customer_id == "unknown":
        logger.warning(f"⚠️ Customer ID still unknown after checking existing participants")
    
    # ========================================================================
    # EVENT HANDLERS
    # ========================================================================
    
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
            
        # 1. ALWAYS SEND TO CCM (Even if bot is muted)
        try:
            asyncio.create_task(send_to_ccm(call_id, customer_id, transcript, "CONNECTOR", ctx.proc.userdata["http_session"]))
            logger.info(f"✅ User transcript queued for CCM: '{transcript[:50]}...'")
        except Exception as e:
            logger.error(f"❌ Failed to queue user transcript to CCM: {e}")
            
        # 2. IF BOT IS MUTED, DON'T PROCESS FURTHER (Silent mode for human agent bridge)
        if bot_muted:
            logger.debug("🔇 BOT IS MUTED - Ignoring user input for AI processing")
            return

        # 3. Check for transfer keywords
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
        if bot_muted:
            logger.info("🔇 BOT IS MUTED - Ignoring speech created event")
            return

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
                asyncio.create_task(send_to_ccm(call_id, customer_id, agent_text, "BOT", ctx.proc.userdata["http_session"]))
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
        if bot_muted:
            return
            
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
        if bot_muted:
            return

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
                    asyncio.create_task(send_to_ccm(call_id, customer_id, agent_text, "BOT", ctx.proc.userdata["http_session"]))
                    logger.info(f"✅ Agent item queued for CCM: '{agent_text[:50]}...'")
                except Exception as e:
                    logger.error(f"❌ Failed to queue agent item to CCM: {e}")

    # START CONNECTION AND SESSION
    logger.info("🚀 Starting agent connection and session...")
    
    # 1. Connect to the room first
    await ctx.connect()
    
    # 2. Start the session with the assistant
    await session.start(room=ctx.room, agent=assistant)
    
    logger.info(f"✅ AGENT CONNECTED AND SESSION STARTED: {call_id}")



    # Wait for the process to finish
    # We use a future to keep the agent alive until the room is disconnected
    shutdown_future = asyncio.Future()
    
    @ctx.room.on("disconnected")
    def on_disconnected(reason):
        logger.info(f"🔌 Room disconnected: {reason}")
        
        # Clean up HTTP session (Async task)
        async def cleanup():
            if "http_session" in ctx.proc.userdata:
                try:
                    await ctx.proc.userdata["http_session"].close()
                    del ctx.proc.userdata["http_session"]
                    logger.info("🌐 Persistent HTTP session closed")
                except Exception as e:
                    logger.error(f"❌ Error during session cleanup: {e}")
        
        asyncio.create_task(cleanup())
            
        if not shutdown_future.done():
            shutdown_future.set_result(None)
            
    await shutdown_future

# ============================================================================
# RUN SERVER
# ============================================================================
if __name__ == "__main__":
    cli.run_app(server)