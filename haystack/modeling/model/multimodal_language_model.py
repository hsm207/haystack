from typing import Tuple, Set, Optional, Union, Dict, Any, Type

import logging
from pathlib import Path
from functools import wraps
from abc import ABC, abstractmethod

import torch
from torch import nn
import transformers
from transformers import AutoConfig, PreTrainedModel
from transformers.modeling_utils import SequenceSummary

from haystack.errors import ModelingError


logger = logging.getLogger(__name__)


def silence_transformers_logs(from_pretrained_func):
    """
    A wrapper that raises the log level of Transformers to
    ERROR to hide some unnecessary warnings.
    """

    @wraps(from_pretrained_func)
    def quiet_from_pretrained_func(cls, *args, **kwargs):

        # Raise the log level of Transformers
        t_logger = logging.getLogger("transformers")
        original_log_level = t_logger.level
        t_logger.setLevel(logging.ERROR)

        result = from_pretrained_func(cls, *args, **kwargs)

        # Restore the log level
        t_logger.setLevel(original_log_level)

        return result

    return quiet_from_pretrained_func


class MultiModalLanguageModel(nn.Module, ABC):
    """
    Parent class for models that can embed different data types into **COMPARABLE** semantic vector spaces.

    These models read feature vectors (generated by a feature extractor) and return vectors that capture
    the meaning of the original data.

    Models inheriting from MultiModalLanguageModel are designed to be used in parallel one with the other
    in multimodal retrieval settings, for example image retrieval from a text query, combined table and
    text retrieval, etc... They must therefore embed their source data into comparable vector spaces.
    """

    @silence_transformers_logs
    def __init__(
        self, pretrained_model_name_or_path: str, model_type: str, model_kwargs: Optional[Dict[str, Any]] = None
    ):
        """
        :param pretrained_model_name_or_path: name of the model to load
        :param model_type: the value of model_type from the model's Config
        :param model_kwargs: dictionary of parameters to pass to the model's initialization (revision, use_auth_key, etc...)
            Haystack applies some default parameters to some models. They can be overridden by users by specifying the
            desired value in this parameter. See `HUGGINGFACE_DEFAULT_MODEL_PARAMS`.
        """
        logger.info(
            f" 🤖 LOADING MODEL: '{pretrained_model_name_or_path}' {'(' + model_type + ')' if model_type else ''}"
        )
        super().__init__()
        self.model_type = model_type

        model_params = HUGGINGFACE_DEFAULT_MODEL_PARAMS.get(model_type, {}) | (model_kwargs or {})
        model_class: PreTrainedModel = getattr(transformers, model_type, None)
        self.model = model_class.from_pretrained(str(pretrained_model_name_or_path), **(model_params or {}))

    @property
    @abstractmethod
    def output_dims():
        """
        The output dimension of this language model
        """
        pass

    @classmethod
    @property
    @abstractmethod
    def expected_inputs(cls) -> Tuple[Set[str], Set[str]]:
        """
        Returns a tuple, (List[mandatory arg names], List[optional arg names])
        """
        pass

    def forward(self, **kwargs) -> torch.Tensor:
        """
        Performs a forward pass of the LM model.

        Validates the inputs according to what the subclass declared in the `expected_inputs` property,
        then hands over the params to the actual model.
        """
        mandatory_args, optional_args = self.expected_inputs
        all_args = mandatory_args | optional_args
        given_args = set(kwargs.keys())
        if not (given_args >= mandatory_args and given_args <= all_args):
            raise ModelingError(
                "The input parameters do not match the model's expectations.\n"
                f"Input names: {', '.join(sorted(kwargs.keys()))}\n"
                f"Expected: {', '.join(sorted(all_args))} (where {', '.join(sorted(mandatory_args))} are mandatory)"
            )
        return self._forward(**kwargs)

    def _forward(self, **kwargs) -> torch.Tensor:
        """
        Hook for subclasses to run their own code before or after the inference.

        The default implementation passes the vectors as they are to the model
        and returns the pooler output of the model's forward pass (assuming the model has
        a pooler and populates the `pooler_output` attribute of its output).
        """
        output = self.model(**kwargs)
        return output.pooler_output


class MultiModalLanguageModelPlusPooler(MultiModalLanguageModel):
    def __init__(
        self,
        pretrained_model_name_or_path: str,
        model_type: str,
        model_kwargs: Optional[Dict[str, Any]] = None,
        pooler_params: Optional[Dict[str, Any]] = {"summary_last_dropout": 0},
    ):
        """
        Support for models that do not come with their own pooler.

        :param pretrained_model_name_or_path: name of the model to load
        :param model_type: the value of model_type from the model's Config
        :param model_kwargs: dictionary of parameters to pass to the model's initialization (revision, use_auth_key, etc...)
        :param pooling_params: the parameters to pass to the pooler. If set, it will create an additional pooler to pass
            the output to. Overwrites the default `{"summary_last_dropout": 0}`.
        """
        super().__init__(
            pretrained_model_name_or_path=pretrained_model_name_or_path,
            model_type=model_type,
            model_kwargs=model_kwargs,
        )
        # These models do not provide a pooled_output by default, so we initialize an extra pooler.
        # The pooler takes the first hidden representation & feeds it to a dense layer of (hidden_dim x hidden_dim).
        # We don't want a dropout in the end of the pooler, since we do that already in the adaptive model before we
        # feed everything to the prediction head
        # FIXME verify the above statement.
        config = self.model.config
        for key, value in pooler_params.items():
            setattr(config, key, value)

        self.pooler = SequenceSummary(config)
        self.pooler.apply(self.model._init_weights)

    def _forward(self, **kwargs):
        """
        Hook for subclasses to run their own code before or after the inference.

        The default implementation passes the vectors as they are to the model
        and returns the pooled output of the model's forward pass (assuming the model has
        a pooler and populates the `pooled_output` attribute of its output).
        """
        output = self.model(**kwargs)
        pooled_output = self.pooler(output)
        return pooled_output


class TextLanguageModel(MultiModalLanguageModel):
    """
    Modeled over facebook/data2vec-text-base. Might not be suitable yet for other text models.
    """

    @classmethod
    @property
    def expected_inputs(cls) -> Tuple[Set[str], Set[str]]:
        return {"input_ids", "token_type_ids", "attention_mask"}, set()

    @property
    def output_dims(self) -> int:
        return self.dim  # "hidden_size", "d_model",


class TextLanguageModelPlusPooler(MultiModalLanguageModelPlusPooler):
    @classmethod
    @property
    def expected_inputs(cls) -> Tuple[Set[str], Set[str]]:
        return {"input_ids", "token_type_ids", "attention_mask"}, set()

    @property
    def output_dims(self) -> int:
        return self.dim  # "hidden_size", "d_model",


class ImageLanguageModel(MultiModalLanguageModel):
    """
    Modeled over facebook/data2vec-vision-base. Might not be suitable yet for other image models.
    """

    @classmethod
    @property
    def expected_inputs(cls) -> Tuple[Set[str], Set[str]]:
        return {"pixel_values"}, {"bool_masked_pos", "head_mask"}

    @property
    def output_dims(self) -> int:
        return self.window_size


#: Match the name of the HuggingFace Model class to the corresponding Haystack wrapper
HUGGINGFACE_TO_HAYSTACK: Dict[str, Type[MultiModalLanguageModel]] = {
    "AutoModel": TextLanguageModel,
    "Data2VecTextForQuestionAnswering": TextLanguageModel,
    "Data2VecVisionForImageClassification": ImageLanguageModel,
}

#: HF Capitalization pairs. Contains alternative capitalizations.
HUGGINGFACE_CAPITALIZE = {
    "data2vec-text": " Data2VecTextForQuestionAnswering",
    "data2vec-vision": "Data2VecVisionForImageClassification",
    **{k.lower(): k for k in HUGGINGFACE_TO_HAYSTACK.keys()},
}

#: Default parameters to be given at init time to some specific models
HUGGINGFACE_DEFAULT_MODEL_PARAMS: Dict[str, Dict[str, Any]] = {
    "Data2VecVisionForImageClassification": {"add_pooling_layer": True}  # Defaults to False, but we need pooled output
}


def get_mm_language_model(
    pretrained_model_name_or_path: Union[Path, str],
    autoconfig_kwargs: Optional[Dict[str, Any]] = None,
    model_kwargs: Optional[Dict[str, Any]] = None,
) -> MultiModalLanguageModel:
    """
    Load a pretrained language model by specifying its name and downloading the model.

    See all supported model variations at: https://huggingface.co/models.
    The appropriate language model class is inferred automatically from model configuration.

    :param pretrained_model_name_or_path: The path of the saved pretrained model or its name.
    :param autoconfig_kwargs: Additional keyword arguments to pass to AutoConfig, like the revision or the auth key.
    :param model_kwargs: Additional keyword arguments to pass to the language model constructor.
        Haystack applies some default parameters to some models. They can be overridden by users by specifying the
        desired value in this parameter. See `HUGGINGFACE_DEFAULT_MODEL_PARAMS`.
    """

    if not pretrained_model_name_or_path or not isinstance(pretrained_model_name_or_path, (str, Path)):
        raise ValueError(f"{pretrained_model_name_or_path} is not a valid pretrained_model_name_or_path parameter")

    model_name = str(pretrained_model_name_or_path)

    # Use AutoConfig to understand the model class
    config = AutoConfig.from_pretrained(pretrained_model_name_or_path=model_name, **(autoconfig_kwargs or {}))
    if not config.model_type:
        logger.error(
            f"Model type not understood for '{model_name}'. Please provide the name of a model that can be "
            f"downloaded from the Model Hub.\nUsing the AutoModel class for '{pretrained_model_name_or_path}'. "
            "This can cause crashes!"
        )

    # Find the HF class corresponding to this model type
    model_type = HUGGINGFACE_CAPITALIZE.get(config.model_type.lower(), "AutoModel")
    language_model_class = HUGGINGFACE_TO_HAYSTACK.get(model_type)
    if not language_model_class:
        raise ValueError(
            f"The type of the given model (name/path: {pretrained_model_name_or_path}, detected type: {model_type}) "
            "is not supported by Haystack or was not correctly identified. Please use supported models only. "
            f"Supported model types: {', '.join(HUGGINGFACE_TO_HAYSTACK.keys())}"
        )

    # Instantiate the model's wrapper
    language_model = language_model_class(
        pretrained_model_name_or_path=pretrained_model_name_or_path, model_type=model_type, model_kwargs=model_kwargs
    )
    return language_model
