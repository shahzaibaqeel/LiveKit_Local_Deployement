import logging, os
from dotenv import load_dotenv
from pathlib import Path
from livekit.agents import *
from livekit.plugins import silero, deepgram, openai, elevenlabs
from livekit.plugins.turn_detector.multilingual import MultilingualModel

# Load .env from same directory
load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

logger = logging.getLogger("agent")

class Assistant(Agent):
    def __init__(self):
        super().__init__(
            instructions="You are a helpful voice AI assistant."
        )

server = AgentServer()

@server.rtc_session(agent_name="inbound-agent")
async def my_agent(ctx: JobContext):
    vad = silero.VAD.load()
    
    session = AgentSession(
        stt=deepgram.STT(model="nova-2-phonecall", language="en-US"),
        llm=openai.LLM(
            model="llama-3.3-70b-versatile",
            base_url="https://api.groq.com/openai/v1"
        ),
        tts=elevenlabs.TTS(
            api_key=os.getenv("ELEVENLABS_API_KEY"),
            model="eleven_multilingual_v2"
        ),
        turn_detection=MultilingualModel(),
        vad=vad
    )
    await session.start(agent=Assistant(), room=ctx.room)
    await ctx.connect()

if __name__ == "__main__":
    cli.run_app(server)