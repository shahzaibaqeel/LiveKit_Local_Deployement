"""
============================================================================
LIVEKIT AGENT WITH OPENAI REALTIME API
Converted from STT+LLM+TTS pipeline to OpenAI Realtime (speech-to-speech)
============================================================================
"""

import logging
from dotenv import load_dotenv
from livekit import rtc
import os
from pathlib import Path
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

# ============================================================================
# AGENT DEFINITION
# ============================================================================
class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions="""You are a helpful voice AI assistant. 
            The user is interacting with you via voice.
            You provide concise, friendly responses without complex formatting or emojis.
            Answer briefly and clearly.""",
        )

# ============================================================================
# SERVER SETUP
# ============================================================================
server = AgentServer()

def prewarm(proc: JobProcess):
    """Prewarm function to load VAD model"""
    proc.userdata["vad"] = silero.VAD.load()

server.setup_fnc = prewarm

# ============================================================================
# MAIN AGENT HANDLER
# ============================================================================
@server.rtc_session(agent_name="")
async def my_agent(ctx: JobContext):
    ctx.log_context_fields = {"room": ctx.room.name}
    
    # ========================================================================
    # OPENAI REALTIME API SESSION
    # This replaces the separate STT + LLM + TTS pipeline
    # ========================================================================
    session = AgentSession(
        # ✅ Use OpenAI Realtime API (replaces STT, LLM, and TTS)
        llm=openai.realtime.RealtimeModel(
            # Model selection
            model="gpt-4o-realtime-preview-2024-12-17",
            
            # Voice selection (alloy, echo, fable, onyx, nova, shimmer, marin, etc.)
            voice="alloy",
            
            # Temperature (0.6-1.2, default 0.8)
            temperature=0.8,
            
            # Modalities: ['text', 'audio'] for full speech-to-speech
            # Use ['text'] if you want to use separate TTS
            modalities=['text', 'audio'],
            
            # Turn detection configuration
            turn_detection={
                "type": "server_vad",  # or "semantic_vad"
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 500,
                "create_response": True,
                "interrupt_response": True,
            },
        ),
        
        # VAD (Voice Activity Detection) - still used for local detection
        vad=ctx.proc.userdata["vad"],
        
        # Preemptive generation for lower latency
        preemptive_generation=True,
    )
    
    # Start the agent session
    await session.start(
        agent=Assistant(),
        room=ctx.room,
    )
    
    # Connect to the room
    await ctx.connect()

# ============================================================================
# RUN THE SERVER
# ============================================================================
if __name__ == "__main__":
    cli.run_app(server)