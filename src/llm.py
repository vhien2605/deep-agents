"""Build the chat model selected by LLM_PROVIDER in .env.

Every provider gets the same decoding setup: the configured temperature and
timeout, a single completion, and by default no token streaming — the agent
consumes whole messages, and streaming only adds a failure mode where a dropped
connection truncates a response mid-tool-call. The chat front end passes
`streaming=True` for its own model so replies appear token by token. No
max_tokens cap is set for OpenAI-compatible endpoints, so a long test file is
never truncated mid-file.
"""

from langchain_core.language_models.chat_models import BaseChatModel

from src import config


def build_chat_model(*, streaming: bool = False) -> BaseChatModel:
    config.require_llm_config()
    provider = config.LLM_PROVIDER

    sampling = {} if config.LLM_TEMPERATURE is None else {"temperature": config.LLM_TEMPERATURE}

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        kwargs = dict(sampling)
        if config.LLM_BASE_URL:
            kwargs["base_url"] = config.LLM_BASE_URL
        return ChatAnthropic(
            model=config.LLM_MODEL,
            api_key=config.LLM_API_KEY,
            timeout=config.LLM_TIMEOUT_SECONDS,
            # The Anthropic API requires max_tokens; use a ceiling high enough
            # that a full test file fits.
            max_tokens=32000,
            disable_streaming=not streaming,
            **kwargs,
        )

    from langchain_openai import ChatOpenAI

    base_url = config.LLM_BASE_URL
    if provider == "openrouter" and not base_url:
        base_url = config.OPENROUTER_BASE_URL
    return ChatOpenAI(
        model=config.LLM_MODEL,
        base_url=base_url,
        api_key=config.LLM_API_KEY,
        **sampling,
        timeout=config.LLM_TIMEOUT_SECONDS,
        n=1,
        disable_streaming=not streaming,
    )
