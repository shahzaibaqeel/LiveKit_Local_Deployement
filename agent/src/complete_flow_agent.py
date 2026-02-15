"""
============================================================================
LIVEKIT AGENT - DIAGNOSTIC VERSION
Let's see what events are actually firing!
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
# CCM API HELPER
# ============================================================================
async def send_to_ccm(call_id: str, customer_id: str, message: str, sender_type: str):
    """Send transcript to CCM API"""
    
    print(f"\n{'='*80}")
    print(f"[CCM SEND] Attempting to send:")
    print(f"  Call ID: {call_id}")
    print(f"  Customer: {customer_id}")
    print(f"  Type: {sender_type}")
    print(f"  Message: {message}")
    print(f"{'='*80}\n")
    
    if not message or not message.strip():
        logger.warning(f"[CCM] Empty message, skipping")
        return False
    
    timestamp = str(int(time.time() * 1000))
    
    channel_data = {
        "channelCustomerIdentifier": customer_id,
        "serviceIdentifier": "9876",
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
    
    print(f"[CCM SEND] Payload prepared, sending to CCM...")
    
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                "https://efcx4-voice.expertflow.com/ccm/message/receive",
                json=payload,
                headers={"Content-Type": "application/json"}
            ) as resp:
                response_text = await resp.text()
                
                print(f"[CCM RESPONSE] Status: {resp.status}")
                print(f"[CCM RESPONSE] Body: {response_text}")
                
                if 200 <= resp.status < 300:
                    print(f"✅ CCM SUCCESS!\n")
                    logger.info(f"✅ CCM SUCCESS [{sender_type}]")
                    return True
                else:
                    print(f"❌ CCM FAILED!\n")
                    logger.error(f"❌ CCM FAILED [{sender_type}]")
                    return False
                    
    except Exception as e:
        print(f"❌ CCM EXCEPTION: {e}\n")
        logger.error(f"❌ CCM ERROR: {e}")
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
    
    print(f"\n{'#'*80}")
    print(f"### NEW CALL STARTED ###")
    print(f"### Room: {call_id}")
    print(f"{'#'*80}\n")
    
    logger.info(f"🔵 NEW CALL: Room={call_id}")
    
    # State
    state = {
        "customer_id": "unknown",
        "transfer_triggered": False,
        "sent_transcripts": set(),
        "event_count": {
            "user_input_transcribed": 0,
            "agent_speech": 0,
            "conversation_item_added": 0,
        }
    }
    
    # ========================================================================
    # CUSTOMER ID EXTRACTION
    # ========================================================================
    def extract_customer_id(identity: str) -> str:
        """Extract customer ID from participant identity"""
        print(f"[EXTRACT] Input: {identity}")
        
        if identity.startswith("sip_"):
            extracted = identity.replace("sip_", "")
            print(f"[EXTRACT] Extracted: {extracted}")
            
            if extracted == "1005":
                print(f"[EXTRACT] Override: 1005 → 99900")
                return "99900"
            
            return extracted
        
        if identity.startswith("sip:"):
            try:
                extracted = identity.split(":")[1].split("@")[0]
                print(f"[EXTRACT] From URI: {extracted}")
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
        print(f"\n[TRANSFER] Executing transfer...")
        
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
            
            print(f"[TRANSFER] Success: {result.sip_call_id}\n")
            await send_to_ccm(call_id, state["customer_id"], "Transfer initiated", "BOT")
            
        except Exception as e:
            print(f"[TRANSFER] Failed: {e}\n")
            state["transfer_triggered"] = False
    
    # ========================================================================
    # ROOM EVENTS
    # ========================================================================
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        print(f"[ROOM EVENT] participant_connected: {participant.identity}")
        
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            if participant.identity.startswith("sip_"):
                state["customer_id"] = extract_customer_id(participant.identity)
                print(f"[ROOM EVENT] Customer identified: {state['customer_id']}\n")
    
    @ctx.room.on("track_subscribed")
    def on_track_subscribed(track: rtc.Track, publication: rtc.TrackPublication, participant: rtc.RemoteParticipant):
        print(f"[ROOM EVENT] track_subscribed: {participant.identity} - {track.kind}")
        
        if state["customer_id"] == "unknown" and participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            if participant.identity.startswith("sip_"):
                state["customer_id"] = extract_customer_id(participant.identity)
                print(f"[ROOM EVENT] Customer from track: {state['customer_id']}\n")
    
    # ========================================================================
    # CHECK EXISTING PARTICIPANTS
    # ========================================================================
    print(f"[STARTUP] Checking existing participants...")
    for sid, p in ctx.room.remote_participants.items():
        print(f"[STARTUP] Found: {p.identity} (kind: {p.kind})")
        
        if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP and p.identity.startswith("sip_"):
            state["customer_id"] = extract_customer_id(p.identity)
            print(f"[STARTUP] Customer set to: {state['customer_id']}")
            break
    
    print(f"[STARTUP] Final customer_id: {state['customer_id']}\n")
    
    # ========================================================================
    # OPENAI REALTIME SESSION
    # ========================================================================
    print(f"[STARTUP] Creating OpenAI session...")
    
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
    
    print(f"[STARTUP] Session created\n")
    
    # ========================================================================
    # SESSION EVENT HANDLERS
    # ========================================================================
    
    print(f"[STARTUP] Registering event handlers...")
    
    @session.on("user_input_transcribed")
    def on_user_transcribed(event):
        """Customer speaks"""
        state["event_count"]["user_input_transcribed"] += 1
        
        print(f"\n{'*'*80}")
        print(f"[EVENT] user_input_transcribed (count: {state['event_count']['user_input_transcribed']})")
        print(f"[EVENT] is_final: {event.is_final}")
        print(f"[EVENT] transcript: {event.transcript}")
        print(f"{'*'*80}\n")
        
        if not event.is_final:
            print(f"[EVENT] Skipping (not final)\n")
            return
        
        transcript = event.transcript.strip()
        if not transcript:
            print(f"[EVENT] Skipping (empty)\n")
            return
        
        print(f"[EVENT] Processing final transcript: {transcript}")
        print(f"[EVENT] Will send to CCM as CONNECTOR\n")
        
        # Send to CCM
        loop = asyncio.get_event_loop()
        loop.create_task(
            send_to_ccm(call_id, state["customer_id"], transcript, "CONNECTOR")
        )
        
        # Check transfer
        keywords = ["transfer", "human", "agent", "representative", "person", "someone"]
        if any(k in transcript.lower() for k in keywords):
            print(f"[EVENT] Transfer keyword detected!\n")
            loop.create_task(execute_transfer())
    
    @session.on("agent_speech")
    def on_agent_speech(event):
        """AI speaks"""
        state["event_count"]["agent_speech"] += 1
        
        print(f"\n{'*'*80}")
        print(f"[EVENT] agent_speech (count: {state['event_count']['agent_speech']})")
        print(f"[EVENT] Has text attr: {hasattr(event, 'text')}")
        if hasattr(event, 'text'):
            print(f"[EVENT] text: {event.text}")
        print(f"{'*'*80}\n")
        
        if hasattr(event, 'text') and event.text:
            text = event.text.strip()
            if not text:
                print(f"[EVENT] Skipping (empty text)\n")
                return
            
            text_hash = hash(text)
            if text_hash in state["sent_transcripts"]:
                print(f"[EVENT] Skipping (duplicate)\n")
                return
            
            state["sent_transcripts"].add(text_hash)
            print(f"[EVENT] Processing AI response: {text}")
            print(f"[EVENT] Will send to CCM as BOT\n")
            
            loop = asyncio.get_event_loop()
            loop.create_task(
                send_to_ccm(call_id, state["customer_id"], text, "BOT")
            )
    
    @session.on("conversation_item_added")
    def on_conversation_item(event):
        """Backup handler"""
        state["event_count"]["conversation_item_added"] += 1
        
        print(f"\n{'*'*80}")
        print(f"[EVENT] conversation_item_added (count: {state['event_count']['conversation_item_added']})")
        print(f"[EVENT] item.role: {event.item.role if hasattr(event, 'item') else 'N/A'}")
        print(f"{'*'*80}\n")
        
        if not hasattr(event, 'item') or event.item.role != "assistant":
            print(f"[EVENT] Skipping (not assistant)\n")
            return
        
        item = event.item
        text = None
        
        if hasattr(item, 'text_content') and item.text_content:
            text = item.text_content.strip()
            print(f"[EVENT] Found text_content: {text}")
        elif hasattr(item, 'content'):
            if isinstance(item.content, str):
                text = item.content.strip()
                print(f"[EVENT] Found content (str): {text}")
            elif isinstance(item.content, list):
                for c in item.content:
                    if hasattr(c, 'text') and c.text:
                        text = c.text.strip()
                        print(f"[EVENT] Found content (list): {text}")
                        break
        
        if not text:
            print(f"[EVENT] No text found\n")
            return
        
        text_hash = hash(text)
        if text_hash in state["sent_transcripts"]:
            print(f"[EVENT] Skipping (duplicate)\n")
            return
        
        state["sent_transcripts"].add(text_hash)
        print(f"[EVENT] Processing backup AI: {text}")
        print(f"[EVENT] Will send to CCM as BOT\n")
        
        loop = asyncio.get_event_loop()
        loop.create_task(
            send_to_ccm(call_id, state["customer_id"], text, "BOT")
        )
    
    print(f"[STARTUP] Event handlers registered\n")
    
    # ========================================================================
    # START SESSION
    # ========================================================================
    print(f"[STARTUP] Starting session...")
    
    await session.start(
        agent=Assistant(call_id, state["customer_id"]),
        room=ctx.room,
    )
    
    print(f"[STARTUP] Session started")
    
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    
    print(f"[STARTUP] Connected to room")
    print(f"{'#'*80}")
    print(f"### AGENT READY - Waiting for conversation ###")
    print(f"{'#'*80}\n")

# ============================================================================
# RUN SERVER
# ============================================================================
if __name__ == "__main__":
    cli.run_app(server)