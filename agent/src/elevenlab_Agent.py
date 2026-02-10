"""
============================================================================
LIVEKIT SIP TO ELEVENLABS CONVERSATIONAL AI BRIDGE + HUMAN TRANSFER
Connects SIP calls to ElevenLabs Agent via WebSocket - PRODUCTION READY
============================================================================
"""

import logging
import os
import time
import asyncio
import websockets
import base64
import json
import numpy as np
from pathlib import Path
from dotenv import load_dotenv
import aiohttp
from livekit import rtc
from livekit import api
from livekit.agents import (
    AutoSubscribe,  # ✅ CORRECT IMPORT
    JobContext, 
    JobProcess, 
    cli, 
    WorkerOptions
)

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
                    await resp.text()
    except Exception as e:
        logger.error(f"❌ CCM error: {e}")

# ============================================================================
# AUDIO CONVERSION HELPERS
# ============================================================================
def resample_audio(audio_data: bytes, original_rate: int, target_rate: int = 16000) -> bytes:
    """Resample audio to 16kHz (required by ElevenLabs)"""
    try:
        # Convert bytes to numpy array (assuming 16-bit PCM)
        audio_array = np.frombuffer(audio_data, dtype=np.int16)
        
        # If already at target rate, return as is
        if original_rate == target_rate:
            return audio_data
        
        # Calculate resampling ratio
        ratio = target_rate / original_rate
        new_length = int(len(audio_array) * ratio)
        
        # Simple linear interpolation resampling
        indices = np.linspace(0, len(audio_array) - 1, new_length)
        resampled = np.interp(indices, np.arange(len(audio_array)), audio_array)
        
        return resampled.astype(np.int16).tobytes()
    except Exception as e:
        logger.error(f"❌ Resampling error: {e}")
        return audio_data

# ============================================================================
# ELEVENLABS AGENT CONNECTION
# ============================================================================
class ElevenLabsAgentBridge:
    def __init__(self, agent_id: str, call_id: str, customer_id: str):
        self.agent_id = agent_id
        self.call_id = call_id
        self.customer_id = customer_id
        self.websocket = None
        self.conversation_id = None
        self.running = False
        self.transfer_requested = False
        
    async def connect(self):
        """Connect to ElevenLabs Conversational AI WebSocket"""
        api_key = os.getenv("ELEVEN_API_KEY")
        
        if not api_key:
            logger.error("❌ ELEVEN_API_KEY not found in .env file")
            return False
        
        if not self.agent_id or self.agent_id == "your-agent-id":
            logger.error("❌ ELEVENLABS_AGENT_ID not configured in .env file")
            return False
        
        # WebSocket endpoint
        url = f"wss://api.elevenlabs.io/v1/convai/conversation?agent_id={self.agent_id}"
        
        headers = {
            "xi-api-key": api_key
        }
        
        try:
            self.websocket = await websockets.connect(url, extra_headers=headers)
            logger.info(f"🟢 Connected to ElevenLabs Agent: {self.agent_id}")
            self.running = True
            return True
        except Exception as e:
            logger.error(f"❌ Failed to connect to ElevenLabs: {e}")
            logger.error(f"   Check your ELEVEN_API_KEY and ELEVENLABS_AGENT_ID in .env")
            return False
    
    async def send_audio(self, audio_frame: rtc.AudioFrame):
        """Send audio to ElevenLabs agent"""
        if not self.websocket or not self.running:
            return
            
        try:
            # Get audio data
            audio_data = audio_frame.data.tobytes()
            
            # Resample to 16kHz if needed (ElevenLabs requires 16kHz PCM)
            if audio_frame.sample_rate != 16000:
                audio_data = resample_audio(audio_data, audio_frame.sample_rate, 16000)
            
            # Convert to base64
            audio_b64 = base64.b64encode(audio_data).decode('utf-8')
            
            # Send to ElevenLabs - CORRECT FORMAT
            message = {
                "user_audio_chunk": audio_b64
            }
            
            await self.websocket.send(json.dumps(message))
            
        except Exception as e:
            logger.error(f"❌ Error sending audio to ElevenLabs: {e}")
    
    async def receive_events(self, audio_source: rtc.AudioSource):
        """Receive events from ElevenLabs and stream to LiveKit"""
        try:
            while self.running:
                message = await self.websocket.recv()
                data = json.loads(message)
                
                # Get event type
                event_type = data.get("type")
                
                # ============================================================
                # CONVERSATION INITIATION
                # ============================================================
                if event_type == "conversation_initiation_metadata":
                    metadata = data.get("conversation_initiation_metadata_event", {})
                    self.conversation_id = metadata.get("conversation_id")
                    logger.info(f"📞 Conversation started: {self.conversation_id}")
                
                # ============================================================
                # USER TRANSCRIPT (what user said)
                # ============================================================
                elif event_type == "user_transcript":
                    user_message = data.get("user_transcription_event", {})
                    transcript = user_message.get("user_transcript", "")
                    
                    if transcript:
                        logger.info(f"👤 USER: {transcript}")
                        
                        # Send to CCM
                        await send_to_ccm(self.call_id, self.customer_id, transcript, "CONNECTOR")
                        
                        # Check for transfer keywords
                        transfer_keywords = ["transfer", "human", "agent", "representative", "person", "someone", "live agent"]
                        if any(keyword in transcript.lower() for keyword in transfer_keywords):
                            logger.info(f"🔍 TRANSFER KEYWORD DETECTED in: '{transcript}'")
                            self.transfer_requested = True
                
                # ============================================================
                # AGENT RESPONSE (what AI said)
                # ============================================================
                elif event_type == "agent_response":
                    agent_message = data.get("agent_response_event", {})
                    agent_response = agent_message.get("agent_response", "")
                    
                    if agent_response:
                        logger.info(f"🤖 AGENT: {agent_response}")
                        
                        # Send to CCM
                        await send_to_ccm(self.call_id, self.customer_id, agent_response, "BOT")
                
                # ============================================================
                # AUDIO OUTPUT (agent's voice)
                # ============================================================
                elif event_type == "audio":
                    audio_event = data.get("audio_event", {})
                    audio_b64 = audio_event.get("audio_base_64", "")
                    
                    if audio_b64:
                        try:
                            # Decode audio
                            audio_bytes = base64.b64decode(audio_b64)
                            
                            # ElevenLabs sends 16kHz mono PCM
                            # Convert to numpy array
                            audio_array = np.frombuffer(audio_bytes, dtype=np.int16)
                            
                            # Create audio frame for LiveKit
                            samples_per_channel = len(audio_array)
                            
                            audio_frame = rtc.AudioFrame(
                                data=audio_array.tobytes(),
                                sample_rate=16000,
                                num_channels=1,
                                samples_per_channel=samples_per_channel
                            )
                            
                            # Stream to LiveKit
                            await audio_source.capture_frame(audio_frame)
                            
                        except Exception as e:
                            logger.error(f"❌ Error processing audio: {e}")
                
                # ============================================================
                # INTERRUPTION (user interrupted agent)
                # ============================================================
                elif event_type == "interruption":
                    logger.info(f"⚡ User interrupted agent")
                
                # ============================================================
                # PING (keep-alive)
                # ============================================================
                elif event_type == "ping":
                    # Respond with pong
                    pong_message = {
                        "type": "pong",
                        "event_id": data.get("ping_event", {}).get("event_id", 0)
                    }
                    await self.websocket.send(json.dumps(pong_message))
                
        except websockets.exceptions.ConnectionClosed:
            logger.info("🔴 ElevenLabs WebSocket closed")
        except Exception as e:
            logger.error(f"❌ Error receiving from ElevenLabs: {e}", exc_info=True)
        finally:
            self.running = False
    
    async def close(self):
        """Close the WebSocket connection"""
        self.running = False
        if self.websocket:
            await self.websocket.close()
            logger.info("🔴 Disconnected from ElevenLabs")

# ============================================================================
# MAIN AGENT HANDLER
# ============================================================================
async def entrypoint(ctx: JobContext):
    ctx.log_context_fields = {"room": ctx.room.name}
    
    call_id = ctx.room.name
    customer_id = ctx.room.metadata if ctx.room.metadata else "unknown"
    
    logger.info(f"🔵 NEW CALL: Room={call_id}, Customer={customer_id}")
    
    # Get ElevenLabs Agent ID from env
    ELEVENLABS_AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID")
    
    if not ELEVENLABS_AGENT_ID:
        logger.error("❌ ELEVENLABS_AGENT_ID not set in .env file")
        return
    
    transfer_triggered = {"value": False}
    
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
        
        # Close ElevenLabs connection first
        await elevenlabs_bridge.close()
        
        await send_to_ccm(call_id, customer_id, "Connecting you to our live agent...", "BOT")
        
        try:
            livekit_api = api.LiveKitAPI(
                url=os.getenv("LIVEKIT_URL"),
                api_key=os.getenv("LIVEKIT_API_KEY"),
                api_secret=os.getenv("LIVEKIT_API_SECRET")
            )
            
            outbound_trunk_id = "ST_W7jqvDFA2VgG"
            agent_extension = "99900"
            
            logger.info(f"📞 Transferring to: {agent_extension}")
            
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
            logger.info(f"✅ SIP Call ID: {transfer_result.sip_call_id}")
            
            await send_to_ccm(call_id, customer_id, "Transfer completed", "BOT")
            
        except Exception as e:
            logger.error(f"❌ TRANSFER FAILED: {e}", exc_info=True)
            transfer_triggered["value"] = False
    
    # ========================================================================
    # CONNECT TO ROOM - ✅ FIXED: AutoSubscribe now imported correctly
    # ========================================================================
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    logger.info(f"✅ Connected to room: {call_id}")
    
    # Create audio source for ElevenLabs output (16kHz mono)
    audio_source = rtc.AudioSource(16000, 1)
    track = rtc.LocalAudioTrack.create_audio_track("elevenlabs-audio", audio_source)
    await ctx.room.local_participant.publish_track(track)
    logger.info(f"✅ Published audio track")
    
    # Create ElevenLabs bridge
    elevenlabs_bridge = ElevenLabsAgentBridge(ELEVENLABS_AGENT_ID, call_id, customer_id)
    
    # Connect to ElevenLabs
    if not await elevenlabs_bridge.connect():
        logger.error("❌ Failed to connect to ElevenLabs - check your credentials")
        return
    
    # ========================================================================
    # AUDIO STREAMING FROM LIVEKIT TO ELEVENLABS
    # ========================================================================
    @ctx.room.on("track_subscribed")
    def on_track_subscribed(
        track: rtc.Track,
        publication: rtc.TrackPublication,
        participant: rtc.RemoteParticipant,
    ):
        logger.info(f"🎧 Track subscribed from: {participant.identity}")
        
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            audio_stream = rtc.AudioStream(track)
            
            async def forward_audio():
                """Forward audio from LiveKit (user) to ElevenLabs"""
                logger.info(f"🎤 Started forwarding audio to ElevenLabs")
                try:
                    async for frame in audio_stream:
                        if elevenlabs_bridge.running:
                            await elevenlabs_bridge.send_audio(frame)
                except Exception as e:
                    logger.error(f"❌ Error forwarding audio: {e}")
            
            asyncio.create_task(forward_audio())
    
    # ========================================================================
    # RECEIVE FROM ELEVENLABS AND MONITOR FOR TRANSFER
    # ========================================================================
    async def monitor_conversation():
        """Monitor conversation and handle transfer requests"""
        await elevenlabs_bridge.receive_events(audio_source)
        
        # Check if transfer was requested
        if elevenlabs_bridge.transfer_requested and not transfer_triggered["value"]:
            logger.info(f"🚀 Transfer requested, executing...")
            await execute_transfer()
    
    # Start monitoring
    monitor_task = asyncio.create_task(monitor_conversation())
    
    # Wait for completion
    try:
        await monitor_task
    except Exception as e:
        logger.error(f"❌ Error in conversation: {e}", exc_info=True)
    
    logger.info(f"🔴 Call ended: {call_id}")

# ============================================================================
# RUN SERVER
# ============================================================================
if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
        )
    )