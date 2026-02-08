# ===========================================================================
# FILE: custom_ollama_llm.py - FIXED VERSION FOR LIVEKIT 1.3.10
# ===========================================================================
import asyncio
import logging
from typing import Any
import ollama
from livekit.agents import llm, APIConnectOptions, utils

logger = logging.getLogger("custom-ollama-llm")


class OllamaLLM(llm.LLM):
    """Custom Ollama LLM"""

    def __init__(self, model: str = "gemma3:1b", temperature: float = 0.5, host: str = "http://localhost:11434"):
        super().__init__()
        self._model = model
        self._temperature = temperature
        self._host = host
        self._client = ollama.Client(host=host)
        logger.info(f"✓ Ollama LLM (gemma3:1b) initialized: {model}")

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        conn_options: APIConnectOptions | None = None,
        temperature: float | None = None,
        n: int | None = None,
        parallel_tool_calls: bool | None = None,
        tool_choice: Any = None,
        tools: list | None = None,
    ) -> "LLMStream":
        return LLMStream(
            llm=self,
            client=self._client,
            model=self._model,
            chat_ctx=chat_ctx,
            conn_options=conn_options or APIConnectOptions(),
            temperature=temperature or self._temperature,
            tools=tools,
        )


class LLMStream(llm.LLMStream):
    """Ollama streaming - FIXED ChatChunk structure"""

    def __init__(
        self, 
        llm: OllamaLLM, 
        client: ollama.Client, 
        model: str, 
        chat_ctx: llm.ChatContext,
        conn_options: APIConnectOptions, 
        temperature: float,
        tools: list | None,
    ):
        super().__init__(
            llm=llm, 
            chat_ctx=chat_ctx, 
            conn_options=conn_options,
            tools=tools,
        )
        self._client = client
        self._model = model
        self._temperature = temperature

    async def _run(self):
        try:
            # ✅ CORRECT: Access messages via items property
            messages_list = list(self._chat_ctx.items)
            
            logger.info(f"📨 LLM received {len(messages_list)} items from context")

            # Convert to Ollama format
            messages = []
            for item in messages_list:
                # Only process ChatMessage items
                if not hasattr(item, 'role'):
                    continue
                    
                role = "user"
                role_str = str(item.role).lower()
                if "assistant" in role_str:
                    role = "assistant"
                elif "system" in role_str:
                    role = "system"
                
                content = ""
                if hasattr(item, 'content'):
                    if isinstance(item.content, str):
                        content = item.content
                    elif isinstance(item.content, list):
                        for c in item.content:
                            if hasattr(c, 'text'):
                                content += c.text
                            elif isinstance(c, str):
                                content += c
                
                if content and content.strip():
                    messages.append({"role": role, "content": content.strip()})

            if not messages:
                logger.warning("⚠️ No messages found! Using fallback")
                messages.append({
                    "role": "system",
                    "content": "You are a helpful voice assistant. Respond concisely in 1-2 sentences."
                })

            logger.info(f"📤 Sending {len(messages)} messages to Ollama")

            loop = asyncio.get_event_loop()
            
            def _sync_stream():
                return self._client.chat(
                    model=self._model,
                    messages=messages,
                    stream=True,
                    options={
                        "temperature": self._temperature,
                        "num_predict": 50,  # ← Limit to ~50 tokens (2-3 sentences)
                    }
                )

            stream = await loop.run_in_executor(None, _sync_stream)

            # ✅ KEY FIX: Use correct ChatChunk structure
            # ChatChunk has 'id' and 'delta' fields directly (not 'choices')
            chunk_count = 0
            request_id = utils.shortuuid()  # Generate once for this stream
            
            for chunk in stream:
                content = chunk.get("message", {}).get("content", "")
                if content:
                    chunk_count += 1
                    # Correct structure: ChatChunk(id=..., delta=ChoiceDelta(...))
                    self._event_ch.send_nowait(
                        llm.ChatChunk(
                            id=request_id,
                            delta=llm.ChoiceDelta(
                                role="assistant",
                                content=content
                            )
                        )
                    )

            logger.info(f"✓ LLM completed ({chunk_count} chunks)")

        except Exception as e:
            logger.error(f"❌ Ollama error: {e}", exc_info=True)
            raise