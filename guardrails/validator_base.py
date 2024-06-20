import inspect
import logging
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from string import Template
from typing import Any, Callable, Dict, List, Optional, Type, Union, cast
from warnings import warn

import nltk
import requests
from langchain_core.messages import BaseMessage
from langchain_core.runnables import Runnable, RunnableConfig
from typing_extensions import deprecated

from guardrails.actions.filter import Filter
from guardrails.actions.refrain import Refrain
from guardrails.classes import ErrorSpan  # noqa
from guardrails.classes import PassResult  # noqa
from guardrails.classes import FailResult, InputType, ValidationResult
from guardrails.classes.credentials import Credentials
from guardrails.constants import hub
from guardrails.errors import ValidationError
from guardrails.hub_token.token import get_jwt_token
from guardrails.logger import logger
from guardrails.remote_inference import remote_inference
from guardrails.types.on_fail import OnFailAction

VALIDATOR_IMPORT_WARNING = """Accessing `{validator_name}` using
`from guardrails.validators import {validator_name}` is deprecated and
support will be removed after version 0.5.x. Please switch to the Guardrails Hub syntax:
`from guardrails.hub import {hub_validator_name}` for future updates and support.
For additional details, please visit: {hub_validator_url}.
"""

# Old names -> New names + hub URLs
VALIDATOR_NAMING = {
    "bug-free-python": [
        "ValidPython",
        "https://hub.guardrailsai.com/validator/reflex/valid_python",
    ],
    "bug-free-sql": [
        "ValidSQL",
        "https://hub.guardrailsai.com/validator/guardrails/valid_sql",
    ],
    "competitor-check": [
        "CompetitorCheck",
        "https://hub.guardrailsai.com/validator/guardrails/competitor_check",
    ],
    "detect-secrets": [
        "SecretsPresent",
        "https://hub.guardrailsai.com/validator/guardrails/secrets_present",
    ],
    "is-reachable": [
        "EndpointIsReachable",
        "https://hub.guardrailsai.com/validator/guardrails/endpoint_is_reachable",
    ],
    "ends-with": [
        "EndsWith",
        "https://hub.guardrailsai.com/validator/guardrails/ends_with",
    ],
    "exclude-sql-predicates": [
        "ExcludeSqlPredicates",
        "https://hub.guardrailsai.com/validator/guardrails/exclude_sql_predicates",
    ],
    "extracted-summary-sentences-match": [
        "ExtractedSummarySentencesMatch",
        "https://hub.guardrailsai.com/validator/guardrails/extracted_summary_sentences_match",  # noqa: E501
    ],
    "extractive-summary": [
        "ExtractiveSummary",
        "https://hub.guardrailsai.com/validator/aryn/extractive_summary",
    ],
    "is-high-quality-translation": [
        "HighQualityTranslation",
        "https://hub.guardrailsai.com/validator/brainlogic/high_quality_translation",
    ],
    "is-profanity-free": [
        "ProfanityFree",
        "https://hub.guardrailsai.com/validator/guardrails/profanity_free",
    ],
    "lower-case": [
        "LowerCase",
        "https://hub.guardrailsai.com/validator/guardrails/lowercase",
    ],
    "on_topic": [
        "RestrictToTopic",
        "https://hub.guardrailsai.com/validator/tryolabs/restricttotopic",
    ],
    "one-line": [
        "OneLine",
        "https://hub.guardrailsai.com/validator/guardrails/one_line",
    ],
    "pii": [
        "DetectPII",
        "https://hub.guardrailsai.com/validator/guardrails/detect_pii",
    ],
    "provenance-v0": [
        "ProvenanceEmbeddings",
        "https://hub.guardrailsai.com/validator/guardrails/provenance_embeddings",
    ],
    "provenance-v1": [
        "ProvenanceLLM",
        "https://hub.guardrailsai.com/validator/guardrails/provenance_llm",
    ],
    "qa-relevance-llm-eval": [
        "QARelevanceLLMEval",
        "https://hub.guardrailsai.com/validator/guardrails/qa_relevance_llm_eval",
    ],
    "reading-time": [
        "ReadingTime",
        "https://hub.guardrailsai.com/validator/guardrails/reading_time",
    ],
    "regex_match": [
        "RegexMatch",
        "https://hub.guardrailsai.com/validator/guardrails/regex_match",
    ],
    "remove-redundant-sentences": [
        "RedundantSentences",
        "https://hub.guardrailsai.com/validator/guardrails/redundant_sentences",
    ],
    "saliency-check": [
        "SaliencyCheck",
        "https://hub.guardrailsai.com/validator/guardrails/saliency_check",
    ],
    "similar-to-document": [
        "SimilarToDocument",
        "https://hub.guardrailsai.com/validator/guardrails/similar_to_document",
    ],
    "similar-to-list": [
        "SimilarToPreviousValues",
        "https://hub.guardrailsai.com/validator/guardrails/similar_to_previous_values",
    ],
    "sql-column-presence": [
        "SqlColumnPresence",
        "https://hub.guardrailsai.com/validator/numbersstation/sql_column_presence",
    ],
    "toxic-language": [
        "ToxicLanguage",
        "https://hub.guardrailsai.com/validator/guardrails/toxic_language",
    ],
    "two-words": [
        "TwoWords",
        "https://hub.guardrailsai.com/validator/guardrails/two_words",
    ],
    "upper-case": [
        "UpperCase",
        "https://hub.guardrailsai.com/validator/guardrails/uppercase",
    ],
    "valid-choices": [
        "ValidChoices",
        "https://hub.guardrailsai.com/validator/guardrails/valid_choices",
    ],
    "length": [
        "ValidLength",
        "https://hub.guardrailsai.com/validator/guardrails/valid_length",
    ],
    "valid-range": [
        "ValidRange",
        "https://hub.guardrailsai.com/validator/guardrails/valid_range",
    ],
    "valid-url": [
        "ValidURL",
        "https://hub.guardrailsai.com/validator/guardrails/valid_url",
    ],
    "pydantic_field_validator": [],
}


def split_sentence_str(chunk: str):
    """A naive sentence splitter that splits on periods."""
    if "." not in chunk:
        return []
    fragments = chunk.split(".")
    return [fragments[0] + ".", ".".join(fragments[1:])]


def split_sentence_nltk(chunk: str):
    """
    NOTE: this approach currently does not work
    Use a sentence tokenizer to split the chunk into sentences.

    Because using the tokenizer is expensive, we only use it if there
    is a period present in the chunk.
    """
    # using the sentence tokenizer is expensive
    # we check for a . to avoid wastefully calling the tokenizer
    if "." not in chunk:
        return []
    sentences = nltk.sent_tokenize(chunk)
    if len(sentences) == 0:
        return []
    # return the sentence
    # then the remaining chunks that aren't finished accumulating
    return [sentences[0], "".join(sentences[1:])]


def check_refrain_in_list(schema: List) -> bool:
    """Checks if a Refrain object exists in a list.

    Args:
        schema: A list that can contain lists, dicts or scalars.

    Returns:
        bool: True if a Refrain object exists in the list.
    """
    for item in schema:
        if isinstance(item, Refrain):
            return True
        elif isinstance(item, list):
            if check_refrain_in_list(item):
                return True
        elif isinstance(item, dict):
            if check_refrain_in_dict(item):
                return True

    return False


def check_refrain_in_dict(schema: Dict) -> bool:
    """Checks if a Refrain object exists in a dict.

    Args:
        schema: A dict that can contain lists, dicts or scalars.

    Returns:
        True if a Refrain object exists in the dict.
    """
    for key, value in schema.items():
        if isinstance(value, Refrain):
            return True
        elif isinstance(value, list):
            if check_refrain_in_list(value):
                return True
        elif isinstance(value, dict):
            if check_refrain_in_dict(value):
                return True

    return False


def check_refrain(schema: Union[List, Dict]) -> bool:
    if isinstance(schema, List):
        return check_refrain_in_list(schema)
    return check_refrain_in_dict(schema)


def filter_in_list(schema: List) -> List:
    """Remove out all Filter objects from a list.

    Args:
        schema: A list that can contain lists, dicts or scalars.

    Returns:
        A list with all Filter objects removed.
    """
    filtered_list = []

    for item in schema:
        if isinstance(item, Filter):
            pass
        elif isinstance(item, list):
            filtered_item = filter_in_list(item)
            if len(filtered_item):
                filtered_list.append(filtered_item)
        elif isinstance(item, dict):
            filtered_dict = filter_in_dict(item)
            if len(filtered_dict):
                filtered_list.append(filtered_dict)
        else:
            filtered_list.append(item)

    return filtered_list


def filter_in_dict(schema: Dict) -> Dict:
    """Remove out all Filter objects from a dictionary.

    Args:
        schema: A dictionary that can contain lists, dicts or scalars.

    Returns:
        A dictionary with all Filter objects removed.
    """
    filtered_dict = {}

    for key, value in schema.items():
        if isinstance(value, Filter):
            pass
        elif isinstance(value, list):
            filtered_item = filter_in_list(value)
            if len(filtered_item):
                filtered_dict[key] = filtered_item
        elif isinstance(value, dict):
            filtered_dict[key] = filter_in_dict(value)
        else:
            filtered_dict[key] = value

    return filtered_dict


def filter_in_schema(schema: Union[Dict, List]) -> Union[Dict, List]:
    if isinstance(schema, List):
        return filter_in_list(schema)
    return filter_in_dict(schema)


validators_registry: Dict[str, Type["Validator"]] = {}
types_to_validators = defaultdict(list)


def validator_factory(name: str, validate: Callable) -> Type["Validator"]:
    def validate_wrapper(self, *args, **kwargs):
        return validate(*args, **kwargs)

    validator = type(
        name,
        (Validator,),
        {"validate": validate_wrapper, "rail_alias": name},
    )
    return validator


def register_validator(name: str, data_type: Union[str, List[str]]):
    """Register a validator for a data type."""
    from guardrails.datatypes import types_registry

    if isinstance(data_type, str):
        data_type = types_registry if data_type == "all" else [data_type]
    # Make sure that the data type string exists in the data types registry.
    for dt in data_type:
        if dt not in types_registry:
            raise ValueError(f"Data type {dt} is not registered.")

        types_to_validators[dt].append(name)

    def decorator(cls_or_func: Union[Type[Validator], Callable]):
        """Register a validator for a data type."""
        if isinstance(cls_or_func, type) and issubclass(cls_or_func, Validator):
            cls = cls_or_func
            cls.rail_alias = name
        elif callable(cls_or_func) and not isinstance(cls_or_func, type):
            func = cls_or_func
            func.rail_alias = name  # type: ignore
            # ensure function takes two args
            if not func.__code__.co_argcount == 2:
                raise ValueError(
                    f"Validator function {func.__name__} must take two arguments."
                )
            # dynamically create Validator subclass with `validate` method as `func`
            cls = validator_factory(name, func)
        else:
            raise ValueError(
                "Only functions and Validator subclasses "
                "can be registered as validators."
            )
        validators_registry[name] = cls
        return cls

    return decorator


def get_validator_class(name: Optional[str]) -> Optional[Type["Validator"]]:
    if not name:
        return None
    is_hub_validator = name.startswith(hub)
    validator_key = name.replace(hub, "") if is_hub_validator else name
    registration = validators_registry.get(validator_key)
    if not registration and name.startswith(hub):
        # This should import everything and trigger registration
        # So it should only have to happen once
        # in lieu of completely unregistered validators
        import guardrails.hub  # noqa

        return validators_registry.get(validator_key)

    if not registration:
        warn(f"Validator with id {name} was not found in the registry!  Ignoring...")
        return None

    return registration


@dataclass  # type: ignore
class Validator(Runnable):
    """Base class for validators."""

    rail_alias: str = ""
    # chunking function returns empty list or list of 2 chunks
    # first chunk is the chunk to validate
    # second chunk is incomplete chunk that needs further accumulation
    accumulated_chunks = []
    run_in_separate_process = False
    override_value_on_pass = False
    required_metadata_keys = []
    _metadata = {}

    def __init__(
        self,
        use_local: bool,
        validation_endpoint: str,
        on_fail: Optional[Union[Callable, OnFailAction]] = None,
        **kwargs,
    ):
        # Raise a warning for deprecated validators

        # Get class name and rail_alias
        child_class_name = str(type(self).__name__)
        validator_rail_alias = self.rail_alias
        self.use_local = use_local
        self.validation_endpoint = validation_endpoint
        self.creds = Credentials.from_rc_file()

        if self.use_local is None:
            if not self.creds:
                raise PermissionError(
                    "No credentials found! Please run 'guardrails configure' before"
                    " making any validation requests."
                )
            self.hub_jwt_token = get_jwt_token(self.creds)
            self.use_local = not remote_inference.get_use_remote_inference(self.creds)

        if not self.validation_endpoint:
            validator_id = self.rail_alias.split("/")[-1]
            submission_url = (
                f"{self.validation_endpoint}/validator/{validator_id}/inference"
            )
            self.validation_endpoint = submission_url

        # Check if this rail_alias is deprecated
        if validator_rail_alias in VALIDATOR_NAMING:
            if VALIDATOR_NAMING[validator_rail_alias]:
                warn(
                    VALIDATOR_IMPORT_WARNING.format(
                        validator_name=child_class_name,
                        hub_validator_name=VALIDATOR_NAMING[validator_rail_alias][0],
                        hub_validator_url=VALIDATOR_NAMING[validator_rail_alias][1],
                    ),
                    FutureWarning,
                )
            else:
                warn(
                    f"""{child_class_name} is deprecated and
                    will be removed after version 0.5.x.
                    """,
                    FutureWarning,
                )
        self.on_fail_descriptor: Union[str, OnFailAction] = "custom"

        if on_fail is None:
            on_fail = OnFailAction.NOOP
        if isinstance(on_fail, OnFailAction):
            self.on_fail_descriptor = on_fail
            self.on_fail_method = None
        elif (
            isinstance(on_fail, str)
            and OnFailAction.__members__.get(on_fail.upper()) is not None
        ):
            self.on_fail_descriptor = (
                OnFailAction.__members__.get(on_fail.upper())
                or ""  # this default isn't needed, it's just for pyright
            )
            self.on_fail_method = None
        else:
            self.on_fail_method = on_fail

        # Store the kwargs for the validator.
        self._kwargs = kwargs

        assert (
            self.rail_alias in validators_registry
        ), f"Validator {self.__class__.__name__} is not registered. "

    @staticmethod
    def _post_install(self):
        """Hook for post-install operations. Install local models, cache data, etc."""
        raise NotImplementedError

    def _validate(self, value: Any, metadata: Dict[str, Any]) -> ValidationResult:
        """User implementable function.

        Validates a value and return a validation result. This method should call
        _inference() in the implementation to perform inference on some input
        value.
        """
        raise NotImplementedError

    def _inference_local(self, model_input: Any) -> Any:
        """User implementable function.

        Runs a machine learning pipeline on some input on the local machine. This
        function should receive the expected input to the ML model, and output the
        results from the ml model."""
        raise NotImplementedError

    def _inference_remote(self, model_input: Any) -> Any:
        """User implementable function.

        Runs a machine learning pipeline on some input on a remote machine. This
        function should receive the expected input to the ML model, and output the
        results from the ml model.

        Can call _hub_inference_request() if request is routed through the hub."""
        raise NotImplementedError

    def validate(self, value: Any, metadata: Dict[str, Any]) -> ValidationResult:
        """Do not override this function, instead implement _validate().

        External facing validate function. This function acts as a wrapper for
        _validate() and is intended to apply any meta-validation requirements, logic,
        or pre/post processing."""
        return self._validate(value, metadata)

    def _inference(self, model_input: Any) -> Any:
        """Calls either a local or remote inference engine for use in the validation
        call.

        Args:
            model_input (Any): Receives the input to be passed to your ML model.

        Returns:
            Any: Returns the output from the ML model inference.
        """
        # Only use if both are set, otherwise fall back to local inference
        if self.use_local:
            logger.debug(
                f"{self.rail_alias} either has no hub authentication token or has not "
                "enabled remote inference execution. This validator will use a local "
                "inference engine."
            )
            return self._inference_local(model_input)

        logger.debug(
            f"{self.rail_alias} has found a Validator Hub Service token."
            " Using a remote inference engine."
        )
        return self._inference_remote(model_input)

    def _chunking_function(self, chunk: str) -> List[str]:
        """The strategy used for chunking accumulated text input into validation sets.

        Args:
            chunk (str): The text to chunk into some subset.

        Returns:
            list[str]: The text chunked into some subset.
        """
        return split_sentence_str(chunk)

    def validate_stream(
        self, chunk: Any, metadata: Dict[str, Any], **kwargs
    ) -> Optional[ValidationResult]:
        """Validates a chunk emitted by an LLM. If the LLM chunk is smaller
        than the validator's chunking strategy, it will be accumulated until it
        reaches the desired size. In the meantime, the validator will return
        None.

        If the LLM chunk is larger than the validator's chunking
        strategy, it will split it into validator-sized chunks and
        validate each one, returning an array of validation results.

        Otherwise, the validator will validate the chunk and return the
        result.
        """
        # combine accumulated chunks and new [:-1]chunk
        self.accumulated_chunks.append(chunk)
        accumulated_text = "".join(self.accumulated_chunks)
        # check if enough chunks have accumulated for validation
        split_contents = self._chunking_function(accumulated_text)

        # if remainder kwargs is passed, validate remainder regardless
        remainder = kwargs.get("remainder", False)
        if remainder:
            split_contents = [accumulated_text, ""]
        if len(split_contents) == 0:
            return PassResult()
        [chunk_to_validate, new_accumulated_chunks] = split_contents
        self.accumulated_chunks = [new_accumulated_chunks]
        # exclude last chunk, because it may not be a complete chunk
        validation_result = self.validate(chunk_to_validate, metadata)
        # if validate doesn't set validated chunk, we set it
        if validation_result.validated_chunk is None:
            validation_result.validated_chunk = chunk_to_validate
        return validation_result

    def _hub_inference_request(self, request_body: dict) -> Any:
        """Makes a request to the Validator Hub to run a ML based validation model. This
        request is authed through the hub and rerouted to a hosted ML model. The reply
        from the hosted endpoint is returned and sent to this client.


        Args:
            request_body (dict): A dictionary containing the required info for the final
            inference endpoint to run.

        Raises:
            HttpError: If the recieved reply was not ok.

        Returns:
            Any: Post request response from the ML based validation model.
        """

        try:
            submission_url = self.validation_endpoint

            headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            }
            req = requests.post(submission_url, json=request_body, headers=headers)

            body = req.json()
            if not req.ok:
                logging.error(req.status_code)
                logging.error(body.get("message"))

            return body

        except Exception as e:
            logging.error(
                "An unexpected validation error occurred" f" in {self.rail_alias}: ", e
            )

    def to_prompt(self, with_keywords: bool = True) -> str:
        """Convert the validator to a prompt.

        E.g. ValidLength(5, 10) -> "length: 5 10" when with_keywords is False.
        ValidLength(5, 10) -> "length: min=5 max=10" when with_keywords is True.

        Args:
            with_keywords: Whether to include the keyword arguments in the prompt.

        Returns:
            A string representation of the validator.
        """
        if not len(self._kwargs):
            return self.rail_alias

        kwargs = self._kwargs.copy()
        for k, v in kwargs.items():
            if not isinstance(v, str):
                kwargs[k] = str(v)

        params = " ".join(list(kwargs.values()))
        if with_keywords:
            params = " ".join([f"{k}={v}" for k, v in kwargs.items()])
        return f"{self.rail_alias}: {params}"

    def to_xml_attrib(self):
        """Convert the validator to an XML attribute."""

        if not len(self._kwargs):
            return self.rail_alias

        validator_args = []
        init_args = inspect.getfullargspec(self.__init__)
        for arg in init_args.args[1:]:
            if arg not in ("on_fail", "args", "kwargs"):
                arg_value = self._kwargs.get(arg)
                str_arg = str(arg_value)
                if str_arg is not None:
                    str_arg = "{" + str_arg + "}" if " " in str_arg else str_arg
                    validator_args.append(str_arg)

        params = " ".join(validator_args)
        return f"{self.rail_alias}: {params}"

    def get_args(self):
        """Get the arguments for the validator."""
        return self._kwargs

    def __call__(self, value):
        result = self.validate(value, {})
        if isinstance(result, FailResult):
            from guardrails.validator_service import ValidatorServiceBase

            validator_service = ValidatorServiceBase()
            return validator_service.perform_correction(
                [result], value, self, self.on_fail_descriptor
            )
        return value

    def __eq__(self, other):
        if not isinstance(other, Validator):
            return False
        return self.to_prompt() == other.to_prompt()

    # TODO: Make this a generic method on an abstract class
    def __stringify__(self):
        template = Template(
            """
            ${class_name} {
                rail_alias: ${rail_alias},
                on_fail: ${on_fail_descriptor},
                run_in_separate_process: ${run_in_separate_process},
                override_value_on_pass: ${override_value_on_pass},
                required_metadata_keys: ${required_metadata_keys},
                kwargs: ${kwargs}
            }"""
        )
        return template.safe_substitute(
            {
                "class_name": self.__class__.__name__,
                "rail_alias": self.rail_alias,
                "on_fail_descriptor": self.on_fail_descriptor,
                "run_in_separate_process": self.run_in_separate_process,
                "override_value_on_pass": self.override_value_on_pass,
                "required_metadata_keys": self.required_metadata_keys,
                "kwargs": self._kwargs,
            }
        )

    @deprecated(
        """'Validator.invoke' is deprecated and will be removed in \
    versions 0.5.x and beyond. Use Validator.to_runnable() instead."""
    )
    def invoke(
        self, input: InputType, config: Optional[RunnableConfig] = None
    ) -> InputType:
        output = BaseMessage(content="", type="")
        str_input = None
        input_is_chat_message = False
        if isinstance(input, BaseMessage):
            input_is_chat_message = True
            str_input = str(input.content)
            output = deepcopy(input)
        else:
            str_input = str(input)

        response = self.validate(str_input, self._metadata)

        if isinstance(response, FailResult):
            raise ValidationError(
                (
                    "The response from the LLM failed validation!"
                    f"{response.error_message}"
                )
            )

        if input_is_chat_message:
            output.content = str_input
            return cast(InputType, output)
        return cast(InputType, str_input)

    """
    This method allows the user to provide metadata to validators used in an LCEL chain.
    This is necessary because they can't pass metadata directly to `validate` in a chain
        because is called internally during `invoke`.

    Usage
    ---
    my_validator = Validator(args).with_metadata({ "key": "value" })

    chain = prompt | model | my_validator | output_parser
    chain.invoke({...})

    When called multiple times on the same validator instance,
        the metadata value will be override.
    This allows the user to change the metadata programmatically
        for different chains or calls.
    """

    def with_metadata(self, metadata: Dict[str, Any]):
        """Assigns metadata to this validator to use during validation."""
        self._metadata = metadata
        return self

    def to_runnable(self) -> Runnable:
        from guardrails.integrations.langchain.validator_runnable import (
            ValidatorRunnable,
        )

        return ValidatorRunnable(self)
