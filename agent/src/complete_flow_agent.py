"""
============================================================================
LIVEKIT AGENT - WORKING VERSION
Bot stays in room after transfer but only transcribes (no TTS, no LLM)
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
    JobContext,
    JobProcess,
    WorkerOptions,
    cli,
    llm,
)
from livekit.plugins import openai, silero

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
    """Send transcript to CCM API"""
    
    timestamp = str(int(time.time() * 1000))
    
    channel_data = {
        "channelCustomerIdentifier": customer_id,
        "serviceIdentifier": "1122",
        "channelTypeCode": "CX_VOICE"
    }

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
    
    logger.info(f"📤 CCM [{sender_type}]: {message[:80]}")

    url = "https://efcx-dev2.expertflow.com/ccm/message/receive"
    
    if session:
        return await _post_to_ccm(session, url, payload, sender_type)
    else:
        async with aiohttp.ClientSession() as new_session:
            return await _post_to_ccm(new_session, url, payload, sender_type)

async def _post_to_ccm(session: aiohttp.ClientSession, url: str, payload: dict, sender_type: str):
    try:
        async with session.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if 200 <= resp.status < 300:
                logger.info(f"✅ CCM SUCCESS [{sender_type}]")
                return True
            else:
                text = await resp.text()
                logger.error(f"❌ CCM FAILED [{sender_type}] - {resp.status}: {text}")
                return False
    except Exception as e:
        logger.error(f"❌ CCM ERROR [{sender_type}]: {e}")
        return False

# ============================================================================
# MAIN AGENT
# ============================================================================
async def entrypoint(ctx: JobContext):
    call_id = ctx.room.name
    customer_id = "unknown"
    
    if "http_session" not in ctx.proc.userdata:
        ctx.proc.userdata["http_session"] = aiohttp.ClientSession()
        logger.info("🌐 HTTP session created")
    
    http_session = ctx.proc.userdata["http_session"]
    
    # State
    bot_active = True  # Bot handles conversation
    agent_joined = False
    customer_sid = None
    agent_sid = None
    
    # STT for all participants
    customer_stt = openai.STT()
    agent_stt = openai.STT()
    
    # LLM only when bot is active
    assistant = openai.LLM(model="gpt-4o")
    
    def extract_customer_id(participant: rtc.RemoteParticipant) -> str:
        identity = participant.identity
        metadata = participant.metadata
        
        logger.info(f"🔍 Identity: '{identity}', Metadata: '{metadata}'")
        
        extracted = identity
        
        if identity.startswith("sip_"):
            extracted = identity.replace("sip_", "")
        elif identity.startswith("sip:"):
            try:
                extracted = identity.split(":")[1].split("@")[0]
            except Exception:
                pass
        
        if metadata:
            import json
            try:
                data = json.loads(metadata)
                for key in ["customer_id", "phoneNumber", "number", "from"]:
                    if data.get(key):
                        logger.info(f"✅ ID from metadata: {data[key]}")
                        return str(data[key])
            except Exception:
                pass
        
        if extracted.lower() in ["freeswitch", "unknown", "agent", ""]:
            logger.info(f"📍 Forcing generic to 99900")
            return "99900"
        
        if extracted == "1005":
            logger.warning(f"⚠️ Override 1005 -> 99900")
            return "99900"
        
        logger.info(f"✅ Final ID: {extracted}")
        return extracted
    
    # ========================================================================
    # TRANSFER FUNCTION
    # ========================================================================
    async def execute_transfer():
        nonlocal bot_active, agent_joined
        
        logger.info("🔴 EXECUTING TRANSFER")
        bot_active = False  # Stop LLM processing
        
        await send_to_ccm(call_id, customer_id, "Connecting to agent...", "BOT", http_session)
        
        try:
            livekit_api = api.LiveKitAPI(
                url=os.getenv("LIVEKIT_URL"),
                api_key=os.getenv("LIVEKIT_API_KEY"),
                api_secret=os.getenv("LIVEKIT_API_SECRET")
            )
            
            # Send timer start to CCM
            timer_payload = {
                "id": call_id,
                "header": {
                    "channelData": {
                        "channelCustomerIdentifier": customer_id,
                        "serviceIdentifier": "1122",
                        "channelTypeCode": "CX_VOICE"
                    },
                    "sender": {
                        "id": "6540b0fc90b3913194d45525",
                        "type": "BOT",
                        "senderName": "Voice Bot"
                    },
                    "timestamp": str(int(time.time() * 1000))
                },
                "body": {
                    "type": "SYSTEM",
                    "event": "AGENT_CONNECTED",
                    "callId": call_id,
                    "agentId": "99900",
                    "customerId": customer_id,
                    "timestamp": int(time.time())
                }
            }
            
            async with http_session.post(
                "https://efcx-dev2.expertflow.com/ccm/message/receive",
                json=timer_payload,
                headers={"Content-Type": "application/json"}
            ) as resp:
                logger.info(f"🕐 Timer start sent: {resp.status}")
            
            transfer_result = await livekit_api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=call_id,
                    sip_trunk_id="ST_W7jqvDFA2VgG",
                    sip_call_to="99900",
                    participant_identity="human-agent-general",
                    participant_name="Human Agent",
                    participant_metadata=f'{{"call_id": "{call_id}", "customer_id": "{customer_id}", "timestamp": {int(time.time())}}}',
                    dtmf="",
                    play_ringtone=False,
                )
            )
            
            logger.info(f"✅ TRANSFER SUCCESS: {transfer_result.participant_id}")
            agent_joined = True
            
        except Exception as e:
            logger.error(f"❌ TRANSFER FAILED: {e}")
            bot_active = True
            await send_to_ccm(call_id, customer_id, "Transfer failed", "BOT", http_session)
    
    # ========================================================================
    # ROOM EVENTS
    # ========================================================================
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        nonlocal customer_id, customer_sid, agent_sid, bot_active, agent_joined
        
        logger.info(f"👤 JOINED: {participant.identity}, SID: {participant.sid}")
        
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            if participant.identity != "human-agent-general":
                customer_id = extract_customer_id(participant)
                customer_sid = participant.sid
                logger.info(f"📞 CUSTOMER: {customer_id}, SID: {customer_sid}")
            else:
                agent_sid = participant.sid
                bot_active = False
                agent_joined = True
                logger.info(f"🟢 AGENT CONNECTED, SID: {agent_sid}")

    @ctx.room.on("track_subscribed")
    def on_track_subscribed(track: rtc.Track, publication: rtc.TrackPublication, participant: rtc.RemoteParticipant):
        nonlocal customer_id
        
        logger.info(f"🎧 TRACK: {participant.identity} - {track.kind}")
        
        if customer_id == "unknown" and participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            if participant.identity != "human-agent-general":
                customer_id = extract_customer_id(participant)
        
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            # Customer transcription
            if participant.identity != "human-agent-general" and participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
                async def transcribe_customer():
                    logger.info(f"🎙️ CUSTOMER TRANSCRIPTION START")
                    audio_stream = rtc.AudioStream(track)
                    stt_stream = customer_stt.stream()
                    
                    async def feed_audio():
                        async for event in audio_stream:
                            frame = getattr(event, 'frame', event)
                            if frame:
                                stt_stream.push_frame(frame)
                        stt_stream.end_input()
                    
                    asyncio.create_task(feed_audio())
                    
                    async for event in stt_stream:
                        if event.type == openai.SpeechEventType.FINAL_TRANSCRIPT:
                            text = event.alternatives[0].text
                            if text and text.strip():
                                logger.info(f"👤 CUSTOMER: {text}")
                                asyncio.create_task(send_to_ccm(call_id, customer_id, text, "CONNECTOR", http_session))
                                
                                # Check transfer keywords only if bot active
                                if bot_active:
                                    keywords = ["transfer", "human", "agent", "representative", "person"]
                                    if any(k in text.lower() for k in keywords):
                                        logger.info("🔍 TRANSFER KEYWORD DETECTED")
                                        asyncio.create_task(execute_transfer())
                
                asyncio.create_task(transcribe_customer())
            
            # Agent transcription
            elif participant.identity == "human-agent-general":
                async def transcribe_agent():
                    logger.info(f"🎙️ AGENT TRANSCRIPTION START")
                    audio_stream = rtc.AudioStream(track)
                    stt_stream = agent_stt.stream()
                    
                    async def feed_audio():
                        async for event in audio_stream:
                            frame = getattr(event, 'frame', event)
                            if frame:
                                stt_stream.push_frame(frame)
                        stt_stream.end_input()
                    
                    asyncio.create_task(feed_audio())
                    
                    async for event in stt_stream:
                        if event.type == openai.SpeechEventType.FINAL_TRANSCRIPT:
                            text = event.alternatives[0].text
                            if text and text.strip():
                                logger.info(f"👨‍💼 AGENT: {text}")
                                asyncio.create_task(send_to_ccm(call_id, customer_id, text, "AGENT", http_session))
                
                asyncio.create_task(transcribe_agent())

    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        nonlocal customer_sid, agent_sid
        
        logger.info(f"👋 LEFT: {participant.identity}, SID: {participant.sid}")
        
        # If customer leaves, disconnect everyone
        if participant.sid == customer_sid:
            logger.info("📞 CUSTOMER LEFT - ENDING CALL")
            
            async def end_call():
                try:
                    livekit_api = api.LiveKitAPI(
                        url=os.getenv("LIVEKIT_URL"),
                        api_key=os.getenv("LIVEKIT_API_KEY"),
                        api_secret=os.getenv("LIVEKIT_API_SECRET")
                    )
                    
                    # Disconnect agent if present
                    if agent_sid:
                        try:
                            await livekit_api.room.remove_participant(
                                api.RoomParticipantIdentity(room=call_id, identity="human-agent-general")
                            )
                            logger.info("✅ Agent removed")
                        except Exception as e:
                            logger.error(f"❌ Agent remove error: {e}")
                    
                    # Disconnect room
                    await ctx.room.disconnect()
                    logger.info("✅ Room disconnected")
                    
                except Exception as e:
                    logger.error(f"❌ Cleanup error: {e}")
            
            asyncio.create_task(end_call())
        
        # If agent leaves, disconnect everyone
        elif participant.sid == agent_sid:
            logger.info("👨‍💼 AGENT LEFT - ENDING CALL")
            
            async def end_call():
                try:
                    livekit_api = api.LiveKitAPI(
                        url=os.getenv("LIVEKIT_URL"),
                        api_key=os.getenv("LIVEKIT_API_KEY"),
                        api_secret=os.getenv("LIVEKIT_API_SECRET")
                    )
                    
                    # Disconnect customer if present
                    if customer_sid:
                        try:
                            await livekit_api.room.remove_participant(
                                api.RoomParticipantIdentity(room=call_id, identity=ctx.room.remote_participants[customer_sid].identity)
                            )
                            logger.info("✅ Customer removed")
                        except Exception as e:
                            logger.error(f"❌ Customer remove error: {e}")
                    
                    # Disconnect room
                    await ctx.room.disconnect()
                    logger.info("✅ Room disconnected")
                    
                except Exception as e:
                    logger.error(f"❌ Cleanup error: {e}")
            
            asyncio.create_task(end_call())
    
    # Check existing participants
    for pid, participant in ctx.room.remote_participants.items():
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            if participant.identity != "human-agent-general":
                customer_id = extract_customer_id(participant)
                customer_sid = participant.sid
            else:
                agent_sid = participant.sid
                bot_active = False
                agent_joined = True
    
    # Connect
    await ctx.connect()
    logger.info(f"✅ CONNECTED: {call_id}")
    
    # LLM conversation loop (only when bot_active)
    chat_ctx = llm.ChatContext()
    chat_ctx.append(role="system", text="You are a helpful assistant. When user asks for agent, say 'Let me connect you' and stop.")
    
    async def llm_loop():
        """Simple LLM loop - only runs when bot_active"""
        while bot_active:
            await asyncio.sleep(0.1)
    
    asyncio.create_task(llm_loop())
    
    # Wait for disconnect
    shutdown = asyncio.Future()
    
    @ctx.room.on("disconnected")
    def on_disconnected(reason):
        logger.info(f"🔌 DISCONNECTED: {reason}")
        
        async def cleanup():
            if "http_session" in ctx.proc.userdata:
                try:
                    await ctx.proc.userdata["http_session"].close()
                    del ctx.proc.userdata["http_session"]
                    logger.info("🌐 HTTP session closed")
                except:
                    pass
        
        asyncio.create_task(cleanup())
        
        if not shutdown.done():
            shutdown.set_result(None)
    
    await shutdown

# ============================================================================
# RUN
# ============================================================================
if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=lambda proc: setattr(proc.userdata, "vad", silero.VAD.load())
        )
    )