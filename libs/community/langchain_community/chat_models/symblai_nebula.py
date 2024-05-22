import json
from json import JSONDecodeError
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional

import requests
from aiohttp import ClientSession
from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import (
    BaseChatModel,
    agenerate_from_stream,
    generate_from_stream,
)
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.pydantic_v1 import Field, SecretStr, root_validator
from langchain_core.utils import convert_to_secret_str, get_from_dict_or_env


def _convert_role(role: str) -> str:
    map = {"ai": "assistant", "human": "human", "chat": "human"}
    if role in map:
        return map[role]
    else:
        raise ValueError(f"Unknown role type: {role}")


def _format_nebula_messages(messages: List[BaseMessage]) -> Dict[str, Any]:
    system = ""
    formatted_messages = []
    for message in messages[:-1]:
        if message.type == "system":
            system = message.content
        else:
            formatted_messages.append(
                {
                    "role": _convert_role(message.type),
                    "text": message.content,
                }
            )

    text = messages[-1].content
    formatted_messages.append({"role": "human", "text": text})
    return {"system_prompt": system, "messages": formatted_messages}


class ChatNebula(BaseChatModel):
    """`Nebula` chat large language model - https://docs.symbl.ai/docs/nebula-llm

    API Reference: https://docs.symbl.ai/reference/nebula-chat

    To use, you should have the environment variable ``NEBULA_API_KEY`` set with your
    API key, or pass it as a named parameter to the constructor.
    To request an API key, visit https://platform.symbl.ai/#/login


    Example:
        .. code-block:: python

            from langchain_community.chat_models import ChatNebula
            from langchain_core.messages import SystemMessage, HumanMessage

            chat = ChatNebula(max_new_tokens=1024, temperature=0.5)

            messages = [
            SystemMessage(
                "You are a helpful assistant that answers general knowledge questions."
            ),
            HumanMessage(
                "Answer the following question. How can I help save the world."
            ),
            ]
            chat.invoke(messages)
    """

    max_new_tokens: int = 1024
    """Denotes the number of tokens to predict per generation."""

    temperature: Optional[float] = 0
    """A non-negative float that tunes the degree of randomness in generation."""

    streaming: bool = False

    nebula_api_url: str = "https://api-nebula.symbl.ai"

    nebula_api_key: Optional[SecretStr] = Field(None, description="Nebula API Token")

    class Config:
        """Configuration for this pydantic object."""

        allow_population_by_field_name = True
        arbitrary_types_allowed = True

    @root_validator()
    def validate_environment(cls, values: Dict) -> Dict:
        """Validate that api key exists in environment."""
        values["nebula_api_key"] = convert_to_secret_str(
            get_from_dict_or_env(values, "nebula_api_key", "NEBULA_API_KEY")
        )
        return values

    @property
    def _llm_type(self) -> str:
        """Return type of chat model."""
        return "nebula-chat"

    @property
    def _api_key(self) -> str:
        if self.nebula_api_key:
            return self.nebula_api_key.get_secret_value()
        return ""

    def _stream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        """Call out to Nebula's chat endpoint."""
        url = f"{self.nebula_api_url}/v1/model/chat/streaming"
        headers = {
            "ApiKey": self._api_key,
            "Content-Type": "application/json",
        }
        formatted_data = _format_nebula_messages(messages=messages)
        payload: Dict[str, Any] = {
            "max_new_tokens": self.max_new_tokens,
            "temperature": self.temperature,
            **formatted_data,
            **kwargs,
        }

        payload = {k: v for k, v in payload.items() if v is not None}
        payload = json.dumps(payload)

        response = requests.request(
            "POST", url, headers=headers, data=payload, stream=True
        )
        response.raise_for_status()

        for chunk_response in response.iter_lines():
            chunk_decoded = chunk_response.decode()[6:]
            try:
                chunk = json.loads(chunk_decoded)
            except JSONDecodeError:
                continue
            token = chunk["delta"]
            cg_chunk = ChatGenerationChunk(message=AIMessageChunk(content=token))
            if run_manager:
                run_manager.on_llm_new_token(token, chunk=cg_chunk)
            yield cg_chunk

    async def _astream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        url = f"{self.nebula_api_url}/v1/model/chat/streaming"
        headers = {"ApiKey": self._api_key, "Content-Type": "application/json"}
        formatted_data = _format_nebula_messages(messages=messages)
        payload: Dict[str, Any] = {
            "max_new_tokens": self.max_new_tokens,
            "temperature": self.temperature,
            **formatted_data,
            **kwargs,
        }

        payload = {k: v for k, v in payload.items() if v is not None}
        payload = json.dumps(payload)

        async with ClientSession() as session:
            async with session.post(
                url, data=payload, headers=headers, stream=True
            ) as response:
                response.raise_for_status()
                async for chunk_response in response.content:
                    chunk_decoded = chunk_response.decode()[6:]
                    try:
                        chunk = json.loads(chunk_decoded)
                    except JSONDecodeError:
                        continue
                    token = chunk["delta"]
                    cg_chunk = ChatGenerationChunk(
                        message=AIMessageChunk(content=token)
                    )
                    if run_manager:
                        await run_manager.on_llm_new_token(token, chunk=cg_chunk)
                    yield cg_chunk

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self.streaming:
            stream_iter = self._stream(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )
            return generate_from_stream(stream_iter)

        url = f"{self.nebula_api_url}/v1/model/chat"
        headers = {"ApiKey": self._api_key, "Content-Type": "application/json"}
        formatted_data = _format_nebula_messages(messages=messages)
        payload: Dict[str, Any] = {
            "max_new_tokens": self.max_new_tokens,
            "temperature": self.temperature,
            **formatted_data,
            **kwargs,
        }

        payload = {k: v for k, v in payload.items() if v is not None}
        payload = json.dumps(payload)

        response = requests.request("POST", url, headers=headers, data=payload)
        response.raise_for_status()
        data = response.json()

        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=data["messages"]))],
            llm_output=data,
        )

    async def _agenerate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self.streaming:
            stream_iter = self._astream(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )
            return await agenerate_from_stream(stream_iter)

        url = f"{self.nebula_api_url}/v1/model/chat"
        headers = {"ApiKey": self._api_key, "Content-Type": "application/json"}
        formatted_data = _format_nebula_messages(messages=messages)
        payload: Dict[str, Any] = {
            "max_new_tokens": self.max_new_tokens,
            "temperature": self.temperature,
            **formatted_data,
            **kwargs,
        }

        payload = {k: v for k, v in payload.items() if v is not None}
        payload = json.dumps(payload)

        async with ClientSession() as session:
            async with session.post(url, data=payload, headers=headers) as response:
                response.raise_for_status()
                data = await response.json()

                return ChatResult(
                    generations=[
                        ChatGeneration(message=AIMessage(content=data["messages"]))
                    ],
                    llm_output=data,
                )
