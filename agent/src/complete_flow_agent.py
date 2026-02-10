"""
============================================================================
LIVEKIT AGENT WITH OPENAI REALTIME API + CALL TRANSFER TO HUMAN AGENT
Bot leaves when human agent joins - Customer and Agent talk directly
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
                "https://efcx4-voice.expertflow.com/ccm",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    logger.info(f"✅ CCM sent: {sender_type}")
                    await resp.text()
    except Exception as e:
        logger.error(f"❌ CCM error: {e}")

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
    customer_id = ctx.room.metadata if ctx.room.metadata else "unknown"
    
    logger.info(f"🔵 NEW CALL: Room={call_id}, Customer={customer_id}")
    
    transfer_triggered = {"value": False}
    human_agent_joined = {"value": False}
    session_obj = {"session": None}
    transcription_task = {"task": None}
    
    # ========================================================================
    # CONTINUOUS TRANSCRIPTION FOR CUSTOMER (After bot leaves)
    # ========================================================================
    async def transcribe_customer_audio():
        """Transcribe customer audio after bot leaves"""
        logger.info("🎤 Starting customer transcription (post-transfer)")
        
        await asyncio.sleep(2)  # Wait for tracks to stabilize
        
        try:
            # Find customer participant (not SIP)
            customer_participant = None
            for participant in ctx.room.remote_participants.values():
                if participant.kind != rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
                    customer_participant = participant
                    logger.info(f"✅ Found customer: {participant.identity}")
                    break
            
            if not customer_participant:
                logger.warning("⚠️ No customer participant found")
                return
            
            # Find customer audio track
            customer_track = None
            for track_pub in customer_participant.track_publications.values():
                if track_pub.track and track_pub.track.kind == rtc.TrackKind.KIND_AUDIO:
                    customer_track = track_pub.track
                    logger.info(f"✅ Found customer audio track")
                    break
            
            if not customer_track:
                logger.warning("⚠️ No customer audio track found")
                return
            
            # Use simple STT with VAD
            from livekit.agents import tokenize, stt as agents_stt
            from livekit.agents.llm import LLM
            
            # Create STT instance - using OpenAI Whisper
            stt_instance = openai.STT()
            
            # Create audio stream
            audio_stream = rtc.AudioStream(customer_track)
            stt_stream = stt_instance.stream()
            
            async def forward_audio():
                async for audio_frame in audio_stream:
                    stt_stream.push_frame(audio_frame)
            
            # Start forwarding audio
            forward_task = asyncio.create_task(forward_audio())
            
            # Process transcriptions
            async for event in stt_stream:
                if event.type == agents_stt.SpeechEventType.FINAL_TRANSCRIPT:
                    text = event.alternatives[0].text.strip()
                    if text:
                        logger.info(f"👤 CUSTOMER (to agent): {text}")
                        await send_to_ccm(call_id, customer_id, text, "CONNECTOR")
            
            forward_task.cancel()
            
        except Exception as e:
            logger.error(f"❌ Customer transcription error: {e}", exc_info=True)
    
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
        logger.info(f"👤 JOINED: {participant.identity}, Kind: {participant.kind}, SID: {participant.sid}")
        
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            logger.info(f"🟢 HUMAN AGENT CONNECTED - BOT WILL NOW LEAVE")
            human_agent_joined["value"] = True
            
            # Stop the bot session
            async def disconnect_bot():
                try:
                    await asyncio.sleep(1)  # Brief delay for stability
                    
                    logger.info("🤖 Bot stopping session...")
                    if session_obj["session"]:
                        await session_obj["session"].aclose()
                    
                    logger.info("🤖 Bot disconnecting from room...")
                    await ctx.disconnect()
                    
                    logger.info("✅ Bot left - Customer and Agent connected")
                    
                    # Start transcribing customer
                    transcription_task["task"] = asyncio.create_task(transcribe_customer_audio())
                    
                except Exception as e:
                    logger.error(f"❌ Error disconnecting bot: {e}")
            
            asyncio.create_task(disconnect_bot())
    
    @ctx.room.on("track_subscribed")
    def on_track_subscribed(track: rtc.Track, publication: rtc.TrackPublication, participant: rtc.RemoteParticipant):
        logger.info(f"🎧 TRACK: {participant.identity} - {track.kind}")
    
    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        logger.info(f"👋 LEFT: {participant.identity}")
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            if transcription_task["task"]:
                transcription_task["task"].cancel()
    
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
    
    session_obj["session"] = session
    
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
        
        logger.info(f"👤 USER TRANSCRIPT (final={is_final}): {transcript}")
        
        # Only process final transcripts
        if not is_final:
            return
            
        # Send to CCM
        asyncio.create_task(send_to_ccm(call_id, customer_id, transcript, "CONNECTOR"))
        
        # Check for transfer keywords
        transfer_keywords = ["transfer", "human", "agent", "representative", "person", "someone"]
        if any(keyword in transcript.lower() for keyword in transfer_keywords):
            logger.info(f"🔍 TRANSFER KEYWORD DETECTED: '{transcript}'")
            logger.info(f"🚀 TRIGGERING TRANSFER...")
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
            logger.info(f"🤖 AGENT: {item.text_content}")
            asyncio.create_task(send_to_ccm(call_id, customer_id, item.text_content, "BOT"))
    
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