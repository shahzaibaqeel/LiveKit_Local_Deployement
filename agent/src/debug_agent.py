
import inspect
from livekit.agents import Agent

print("Inspecting livekit.agents.Agent")
print(f"Parent classes: {Agent.__mro__}")
print(f"__init__ signature: {inspect.signature(Agent.__init__)}")
