from typing import Any, Dict, List, Optional

from pydantic import Field

from guardrails.llm_providers import PromptCallableBase
from guardrails.messages.messages import Messages
from guardrails.utils.pydantic_utils import ArbitraryModel


class Inputs(ArbitraryModel):
    llm_api: Optional[PromptCallableBase] = Field(
        description="The constructed class for calling the LLM.", default=None
    )
    llm_output: Optional[str] = Field(
        description="The string output from an external LLM call"
        "provided by the user via Guard.parse.",
        default=None,
    )
    messages: Optional[Messages] = Field(
        description="The message history provided by the user for chat model calls.",
        default=None,
    )
    prompt_params: Optional[Dict] = Field(
        description="The parameters provided by the user"
        "that will be formatted into the final LLM prompt.",
        default=None,
    )
    num_reasks: int = Field(
        description="The total number of reasks allowed; user provided or defaulted.",
        default=None,
    )
    metadata: Optional[Dict[str, Any]] = Field(
        description="The metadata provided by the user to be used during validation.",
        default=None,
    )
    full_schema_reask: bool = Field(
        description="Whether to perform reasks across the entire schema"
        "or at the field level.",
        default=None,
    )
    stream: Optional[bool] = Field(
        description="Whether to use streaming.",
        default=False,
    )
