# ===========================================================================
# FILE: custom-agent.py - COMPLETE FIXED VERSION
# ===========================================================================
import logging
from dotenv import load_dotenv
from pathlib import Path
from livekit.agents import Agent, AgentSession, AgentServer, JobContext, JobProcess, cli
from livekit.plugins import silero

# Import custom implementations
from custom_vosk_stt import VoskSTT
from custom_gtts import CustomGTTS
from custom_ollama_llm import OllamaLLM

# Load environment
current_dir = Path(__file__).parent
env_file = current_dir / ".env"
load_dotenv(dotenv_path=env_file, override=True)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("agent")

# Model paths
PROJECT_ROOT = Path(__file__).parent.parent
VOSK_MODEL_PATH = str(PROJECT_ROOT / "models" / "vosk" / "vosk-model-small-en-us-0.15")

class Assistant(Agent):
    """Voice AI Assistant"""
    def __init__(self) -> None:
        super().__init__(
            instructions="""You are a helpful voice AI assistant. 
            Provide concise, friendly responses in 3-4 sentences.
            Keep your answers brief and conversational.""",
            # ✅ Enable sentence-by-sentence TTS
            min_endpointing_delay=0.5,  # Start TTS after 0.5s of LLM output
            max_endpointing_delay=1.0,  # Force TTS start after 1s max
        )

server = AgentServer()

def prewarm(proc: JobProcess):
    """Pre-load all models during startup"""
    logger.info("🔄 Prewarming models...")
    
    # VAD for voice activity detection
    proc.userdata["vad"] = silero.VAD.load()
    logger.info("✓ VAD loaded")
    
    # Custom Vosk STT
    proc.userdata["vosk_stt"] = VoskSTT(model_path=VOSK_MODEL_PATH)
    logger.info("✓ Vosk STT initialized")
    
    # Custom gTTS (fixed for proper audio format)
    proc.userdata["gtts"] = CustomGTTS(language="en", tld="com")
    logger.info("✓ gTTS initialized")
    
    # Custom Ollama LLM
    proc.userdata["ollama_llm"] = OllamaLLM(model="gemma3:1b", temperature=0.7)
    logger.info("✓ Ollama LLM initialized")
    
    logger.info("✅ All models prewarmed successfully")

server.setup_fnc = prewarm

@server.rtc_session(agent_name="inbound-agent")
async def my_agent(ctx: JobContext):
    """Main agent entry point"""
    ctx.log_context_fields = {"room": ctx.room.name}
    logger.info(f"🚀 Starting agent for room: {ctx.room.name}")

    try:
        # Create agent session with all components
        session = AgentSession(
            stt=ctx.proc.userdata["vosk_stt"],
            llm=ctx.proc.userdata["ollama_llm"],
            tts=ctx.proc.userdata["gtts"],
            vad=ctx.proc.userdata["vad"],
        )

        # Start the session
        await session.start(agent=Assistant(), room=ctx.room)
        logger.info("✓ Agent session started successfully")
        
        # Send initial greeting
        await session.say(
            "Hello! How can I help you today?",
            allow_interruptions=True
        )
        logger.info("✓ Initial greeting sent")
        
    except Exception as e:
        logger.error(f"❌ Agent session error: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    cli.run_app(server)