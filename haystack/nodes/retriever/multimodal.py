from typing import Iterable, get_args, Union, Optional, Dict, List, Any

import logging
from pathlib import Path

import torch
from tqdm import tqdm
import numpy as np
from PIL import Image
from torch.nn import DataParallel
from sentence_transformers import SentenceTransformer
from transformers import AutoConfig

from haystack.nodes.retriever import BaseRetriever
from haystack.document_stores import BaseDocumentStore
from haystack.modeling.model.multimodal_language_model import get_mm_language_model, get_sentence_tranformers_model
from haystack.modeling.model.feature_extraction import FeatureExtractor
from haystack.errors import NodeError, ModelingError
from haystack.schema import ContentTypes, Document
from haystack.modeling.data_handler.multimodal_samples.text import TextSample

from haystack.modeling.data_handler.multimodal_samples.base import Sample
from haystack.modeling.data_handler.multimodal_samples.image import ImageSample
from haystack.modeling.model.feature_extraction import FeatureExtractor


logger = logging.getLogger(__name__)


class MultiModalRetrieverError(NodeError):
    pass


DOCUMENT_CONVERTERS = {
    # NOTE: Keep this '?' cleaning step, it needs to be double-checked for impact on the inference results.
    "text": lambda doc: doc.content[:-1] if doc.content[-1] == "?" else doc.content,
    "table": lambda doc: " ".join(
        doc.content.columns.tolist() + [cell for row in doc.content.values.tolist() for cell in row]
    ),
    "image": lambda doc: Image.open(doc.content),
}

CAN_EMBED_META = ["text", "table"]

SAMPLES_BY_DATATYPE: Dict[ContentTypes, Sample] = {"text": TextSample, "table": TextSample, "image": ImageSample}


def get_features(
    data: List[Any],
    data_type: ContentTypes,
    feature_extractor: FeatureExtractor,
    extraction_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Return proper features by data type by leveraging Sample classes.
    """
    try:
        sample_class = SAMPLES_BY_DATATYPE[data_type]
    except KeyError as e:
        raise ModelingError(
            f"Data type '{data_type}' not recognized. "
            f"Please select one data type among {', '.join(SAMPLES_BY_DATATYPE.keys())}"
        )
    return sample_class.get_features(
        data=data, feature_extractor=feature_extractor, extraction_params=extraction_params
    )


def get_devices(devices: List[Union[str, torch.device]]) -> List[torch.device]:
    """
    Convert a list of device names into a list of Torch devices,
    depending on the system's configuration and hardware.
    """
    if devices is not None:
        return [torch.device(device) for device in devices]
    elif torch.cuda.is_available():
        return [torch.device(device) for device in range(torch.cuda.device_count())]
    return [torch.device("cpu")]


def flatten(iterable: Any):
    """
    Flatten an arbitrarily nested list. Does not unpack tuples or other Iterables.
    Yields a generator. Use `list()` to compute the full list.

    >> list(flatten([1, 2, 3, [4], [], [[[[[[[[[5]]]]]]]]]]))
    [1, 2, 3, 4, 5]
    >> list(flatten([[1, 2], 3]))
    [1, 2, 3]
    """
    if isinstance(iterable, list):
        for item in iterable:
            yield from flatten(item)
    else:
        yield (iterable)


class MultiModalEmbedder:
    def __init__(
        self,
        embedding_models: Dict[ContentTypes, Union[Path, str]] = {"text": "facebook/data2vec-text-base"},
        feature_extractors_params: Dict[str, Dict[str, Any]] = None,
        batch_size: int = 16,
        embed_meta_fields: List[str] = ["name"],
        progress_bar: bool = True,
        devices: Optional[List[Union[str, torch.device]]] = None,
        use_auth_token: Optional[Union[str, bool]] = None,
    ):
        """
        Init the Retriever and all its models from a local or remote model checkpoint.
        The checkpoint format matches huggingface transformers' model format.

        :param embedding_models: Dictionary matching a local path or remote name of encoder checkpoint with
            the content type it should handle ("text", "table", "image", etc...).
            The format equals the one used by hugging-face transformers' modelhub models.
        :param batch_size: Number of questions or passages to encode at once. In case of multiple gpus, this will be the total batch size.
        :param embed_meta_fields: Concatenate the provided meta fields and text passage / image to a text pair that is
                                  then used to create the embedding.
                                  This is the approach used in the original paper and is likely to improve
                                  performance if your titles contain meaningful information for retrieval
                                  (topic, entities etc.).
        :param similarity_function: Which function to apply for calculating the similarity of query and passage embeddings during training.
                                    Options: `dot_product` (Default) or `cosine`
        :param global_loss_buffer_size: Buffer size for all_gather() in DDP.
                                        Increase if errors like "encoded data exceeds max_size ..." come up
        :param progress_bar: Whether to show a tqdm progress bar or not.
                             Can be helpful to disable in production deployments to keep the logs clean.
        :param devices: List of GPU (or CPU) devices, to limit inference to certain GPUs and not use all available ones
                        These strings will be converted into pytorch devices, so use the string notation described here:
                        https://pytorch.org/docs/simage/tensor_attributes.html?highlight=torch%20device#torch.torch.device
                        (e.g. ["cuda:0"]). Note: as multi-GPU training is currently not implemented for TableTextRetriever,
                        training will only use the first device provided in this list.
        :param use_auth_token:  API token used to download private models from Huggingface. If this parameter is set to `True`,
                                the local token will be used, which must be previously created via `transformer-cli login`.
                                Additional information can be found here https://huggingface.co/transformers/main_classes/model.html#transformers.PreTrainedModel.from_pretrained
        """
        super().__init__()

        self.devices = get_devices(devices)
        if batch_size < len(self.devices):
            logger.warning("Batch size is lower than the number of devices. Not all GPUs will be utilized.")

        self.batch_size = batch_size
        self.progress_bar = progress_bar
        self.embed_meta_fields = embed_meta_fields

        self.feature_extractors_params = {
            content_type: {"max_length": 256} | (feature_extractors_params or {}).get(content_type, {})
            for content_type in get_args(ContentTypes)
        }

        self.feature_extractors = {}
        models = {}
        for content_type, embedding_model in embedding_models.items():

            # SentenceTransformers are much faster, so use them if it's possible
            # FIXME find a way to distinguish them better!
            if embedding_model.startswith("sentence-transformers/"):
                models[content_type] = get_sentence_tranformers_model(
                    pretrained_model_name_or_path=embedding_model,
                    content_type=content_type,
                    model_kwargs={"use_auth_token": use_auth_token},
                )
            else:
                # If it's a regular HF model (i.e. not a sentence-transformers model), it needs a feature extractor
                models[content_type] = get_mm_language_model(
                    pretrained_model_name_or_path=embedding_model,
                    content_type=content_type,
                    autoconfig_kwargs={"use_auth_token": use_auth_token},
                    model_kwargs={"use_auth_token": use_auth_token},
                )
                self.feature_extractors[content_type] = FeatureExtractor(
                    pretrained_model_name_or_path=embedding_model, do_lower_case=True, use_auth_token=use_auth_token
                )

        if len(self.devices) > 1:
            self.models = {
                content_type: DataParallel(model, device_ids=self.devices) for content_type, model in models.items()
            }
        else:
            self.models = {content_type: model.to(self.devices[0]) for content_type, model in models.items()}

    def embed(self, documents: List[Document], batch_size: Optional[int] = None) -> np.ndarray:
        """
        Create embeddings for a list of documents using the relevant encoder for their content type.

        :param documents: Documents to embed
        :return: Embeddings, one per document, in the form of a np.array
        """
        batch_size = batch_size if batch_size is not None else self.batch_size

        all_embeddings = []
        for batch_index in tqdm(
            iterable=range(0, len(documents), batch_size),
            unit=" Docs",
            desc=f"Create embeddings",
            position=1,
            leave=False,
            disable=not self.progress_bar,
        ):
            docs_batch = documents[batch_index : batch_index + batch_size]
            data_by_type = self._docs_to_data(documents=docs_batch)

            features_by_type = {}
            for data_type, data_list in data_by_type.items():

                if not self.feature_extractors.get(data_type, None):
                    # sentence-transformers models don't need this step.
                    features = data_list

                else:
                    # Feature extraction
                    features = get_features(
                        data=data_list,
                        data_type=data_type,
                        feature_extractor=self.feature_extractors[data_type],
                        extraction_params=self.feature_extractors_params.get(data_type, {}),
                    )
                    if not features:
                        raise ModelingError(
                            f"Could not extract features for data of type {data_type}. "
                            f"Check that your feature extractor is correct for this data type:\n{self.feature_extractors}"
                        )
                    for key, features_list in features.items():
                        for feature in features_list:
                            if isinstance(feature, torch.Tensor):
                                features[key] = features[key].to(self.devices[0])

                features_by_type[data_type] = features

                # Get output for each model
                outputs_by_type: Dict[ContentTypes, torch.Tensor] = {}
                for key, inputs in features_by_type.items():

                    model = self.models.get(key)
                    if not model:
                        raise ModelingError(
                            f"Some input tensor were passed for models handling {key} data, "
                            "but no such model was initialized. They will be ignored."
                            f"Initialized models: {', '.join(self.models.keys())}"
                        )

                    if not self.feature_extractors.get(data_type, None):
                        # sentence-transformers models
                        outputs_by_type[key] = self.models[key].encode(inputs, convert_to_tensor=True)
                    else:
                        # Note: **inputs is unavoidable here. Different model types take different input vectors.
                        # Validation of the inputs occurrs in the forward() method.
                        outputs_by_type[key] = self.models[key].forward(**inputs)

                # Check the output sizes
                embedding_sizes = [output.shape[-1] for output in outputs_by_type.values()]
                if not all(embedding_size == embedding_sizes[0] for embedding_size in embedding_sizes):
                    raise ModelingError(
                        "Some of the models are using a different embedding size. They should all match. "
                        f"Embedding sizes by model: "
                        f"{ {name: output.shape[-1] for name, output in outputs_by_type.items()} }"
                    )

                # Combine the outputs in a single matrix
                outputs = torch.stack(list(outputs_by_type.values()))
                embeddings = outputs.view(-1, embedding_sizes[0])
                embeddings = embeddings.cpu()  # .numpy()

            all_embeddings.append(embeddings)

        return np.concatenate(all_embeddings)

    def _docs_to_data(self, documents: List[Document]) -> Dict[ContentTypes, List[Any]]:
        """
        Extract the data to embed from each document and returns them classified by content type.

        :param documents: the documents to prepare fur multimodal embedding.
        :return: a dictionary containing one key for each content type, and a list of data extracted
            from each document, ready to be passed to the feature extractor (for example the content
            of a text document, a linearized table, a PIL image object, etc...)
        """
        docs_data = {key: [] for key in get_args(ContentTypes)}
        for doc in documents:
            try:
                document_converter = DOCUMENT_CONVERTERS[doc.content_type]
            except KeyError as e:
                raise MultiModalRetrieverError(
                    f"Unknown content type '{doc.content_type}'. Known types: {', '.join(get_args(ContentTypes))}"
                ) from e

            data = document_converter(doc)

            if self.embed_meta_fields and doc.content_type in CAN_EMBED_META:
                meta = " ".join(doc.meta or [])
                docs_data[doc.content_type].append(
                    f"{meta} {data}" if meta else data
                )  # They used to be returned as a tuple, verify it still works as intended
            else:
                docs_data[doc.content_type].append(data)

        return {key: values for key, values in docs_data.items() if values}


FilterType = Dict[str, Union[Dict[str, Any], List[Any], str, int, float, bool]]


class MultiModalRetriever(BaseRetriever):
    """
    Retriever that uses a multiple encoder to jointly retrieve among a database consisting of different
    data types. See the original paper for more details:
    Kostić, Bogdan, et al. (2021): "Multi-modal Retrieval of Tables and Texts Using Tri-encoder Models"
    (https://arxiv.org/abs/2108.04049),
    """

    def __init__(
        self,
        document_store: BaseDocumentStore,
        query_type: ContentTypes = "text",
        query_embedding_model: Union[Path, str] = "facebook/data2vec-text-base",
        passage_embedding_models: Dict[ContentTypes, Union[Path, str]] = {"text": "facebook/data2vec-text-base"},
        query_feature_extractor_params: Dict[str, Any] = {"max_length": 64},
        passage_feature_extractors_params: Dict[str, Dict[str, Any]] = {"max_length": 256},
        top_k: int = 10,
        batch_size: int = 16,
        embed_meta_fields: List[str] = ["name"],
        similarity_function: str = "dot_product",
        progress_bar: bool = True,
        devices: Optional[List[Union[str, torch.device]]] = None,
        use_auth_token: Optional[Union[str, bool]] = None,
        scale_score: bool = True,
    ):
        """
        Init the Retriever and all its models from a local or remote model checkpoint.
        The checkpoint format matches huggingface transformers' model format.

        :param document_store: An instance of DocumentStore from which to retrieve documents.
        :param query_embedding_model: Local path or remote name of question encoder checkpoint. The format equals the
                                      one used by hugging-face transformers' modelhub models.
        :param passage_embedding_models: Dictionary matching a local path or remote name of passage encoder checkpoint with
            the content type it should handle ("text", "table", "image", etc...).
            The format equals the one used by hugging-face transformers' modelhub models.
        :param max_seq_len_query:Longest length of each passage/context sequence. Represents the maximum number of tokens for the passage text.
            Longer ones will be cut down.
        :param max_seq_len_passages: Dictionary matching the longest length of each query sequence with the content_type they refer to.
            Represents the maximum number of tokens. Longer ones will be cut down.
        :param top_k: How many documents to return per query.
        :param batch_size: Number of questions or passages to encode at once. In case of multiple gpus, this will be the total batch size.
        :param embed_meta_fields: Concatenate the provided meta fields and text passage / image to a text pair that is
                                  then used to create the embedding.
                                  This is the approach used in the original paper and is likely to improve
                                  performance if your titles contain meaningful information for retrieval
                                  (topic, entities etc.).
        :param similarity_function: Which function to apply for calculating the similarity of query and passage embeddings during training.
                                    Options: `dot_product` (Default) or `cosine`
        :param progress_bar: Whether to show a tqdm progress bar or not.
                             Can be helpful to disable in production deployments to keep the logs clean.
        :param devices: List of GPU (or CPU) devices, to limit inference to certain GPUs and not use all available ones
                        These strings will be converted into pytorch devices, so use the string notation described here:
                        https://pytorch.org/docs/simage/tensor_attributes.html?highlight=torch%20device#torch.torch.device
                        (e.g. ["cuda:0"]). Note: as multi-GPU training is currently not implemented for TableTextRetriever,
                        training will only use the first device provided in this list.
        :param use_auth_token:  API token used to download private models from Huggingface. If this parameter is set to `True`,
                                the local token will be used, which must be previously created via `transformer-cli login`.
                                Additional information can be found here https://huggingface.co/transformers/main_classes/model.html#transformers.PreTrainedModel.from_pretrained
        :param scale_score: Whether to scale the similarity score to the unit interval (range of [0,1]).
                            If true (default) similarity scores (e.g. cosine or dot_product) which naturally have a different value range will be scaled to a range of [0,1], where 1 means extremely relevant.
                            Otherwise raw similarity scores (e.g. cosine or dot_product) will be used.
        """
        super().__init__()

        self.similarity_function = similarity_function
        self.progress_bar = progress_bar
        self.top_k = top_k
        self.scale_score = scale_score

        self.passage_embedder = MultiModalEmbedder(
            embedding_models=passage_embedding_models,
            feature_extractors_params=passage_feature_extractors_params,
            batch_size=batch_size,
            embed_meta_fields=embed_meta_fields,
            progress_bar=progress_bar,
            devices=devices,
            use_auth_token=use_auth_token,
        )

        # Try to reuse the same embedder for queries if there is overlap
        if passage_embedding_models.get(query_type, None) == query_embedding_model:
            self.query_embedder = self.passage_embedder
        else:
            self.query_embedder = MultiModalEmbedder(
                embedding_models={query_type: query_embedding_model},
                feature_extractors_params={query_type: query_feature_extractor_params},
                batch_size=batch_size,
                embed_meta_fields=embed_meta_fields,
                progress_bar=progress_bar,
                devices=devices,
                use_auth_token=use_auth_token,
            )

        self.document_store = document_store

    def retrieve(
        self,
        query: str,
        content_type: ContentTypes = "text",
        filters: Optional[FilterType] = None,
        top_k: Optional[int] = None,
        index: str = None,
        headers: Optional[Dict[str, str]] = None,
        scale_score: bool = None,
    ) -> List[Document]:
        return self.retrieve_batch(
            queries=[query],
            content_type=content_type,
            filters=[filters],
            top_k=top_k,
            index=index,
            headers=headers,
            batch_size=1,
            scale_score=scale_score,
        )[0]

    def retrieve_batch(
        self,
        queries: List[str],
        content_type: ContentTypes = "text",
        filters: Optional[Union[FilterType, List[FilterType]]] = None,
        top_k: Optional[int] = None,
        index: str = None,
        headers: Optional[Dict[str, str]] = None,
        batch_size: Optional[int] = None,
        scale_score: bool = None,
    ) -> List[List[Document]]:
        """
        Scan through documents in DocumentStore and return a small number documents
        that are most relevant to the supplied queries.

        Returns a list of lists of Documents (one list per query).

        This method assumes all queries are of the same data type. Mixed-type query batches (i.e. one image and one text)
        are currently not supported. Please group the queries by type and call `retrieve()` on uniform batches only.

        :param queries: List of query strings.
        :param filters: Optional filters to narrow down the search space to documents whose metadata fulfill certain
                        conditions. Can be a single filter that will be applied to each query or a list of filters
                        (one filter per query).
        :param top_k: How many documents to return per query. Must be > 0
        :param index: The name of the index in the DocumentStore from which to retrieve documents
        :param batch_size: Number of queries to embed at a time. Must be > 0
        :param scale_score: Whether to scale the similarity score to the unit interval (range of [0,1]).
                            If true similarity scores (e.g. cosine or dot_product) which naturally have a different
                            value range will be scaled to a range of [0,1], where 1 means extremely relevant.
                            Otherwise raw similarity scores (e.g. cosine or dot_product) will be used.
        """
        filters_list: List[FilterType]
        if not isinstance(filters, Iterable):
            filters_list = [filters or {}] * len(queries)
        else:
            if len(filters) != len(queries):
                raise MultiModalRetrieverError(
                    "Number of filters does not match number of queries. Please provide as many filters "
                    "as queries, or a single filter that will be applied to all queries."
                )
            filters_list = filters

        top_k = top_k or self.top_k
        index = index or self.document_store.index
        scale_score = scale_score or self.scale_score

        # Embed the queries - we need them into Document format to leverage MultiModalEmbedder.embed()
        query_docs = [Document(content=query, content_type=content_type) for query in queries]
        query_embeddings = self.query_embedder.embed(documents=query_docs, batch_size=batch_size)

        # Query documents by embedding (the actual retrieval step)
        documents = []
        for query_embedding, query_filters in zip(query_embeddings, filters_list):
            docs = self.document_store.query_by_embedding(
                query_emb=query_embedding,
                top_k=top_k,
                filters=query_filters,
                index=index,
                headers=headers,
                scale_score=scale_score,
            )

            # docs = custom_query_by_embedding(
            #     self.document_store,
            #     self.passage_embedder.model.models["image"].logit_scale,
            #     query_emb=query_embedding,
            #     top_k=top_k,
            #     filters=query_filters,
            #     scale_score=scale_score,
            # )

            documents.append(docs)
        return documents

    def embed_documents(self, docs: List[Document]) -> np.ndarray:
        return self.passage_embedder.embed(documents=docs)


from copy import deepcopy


def custom_query_by_embedding(
    docstore,
    logit_scale,
    query_emb: np.ndarray,
    filters: Optional[Dict[str, Any]] = None,  # TODO: Adapt type once we allow extended filters in InMemoryDocStore
    top_k: int = 10,
    scale_score: bool = True,
):
    if query_emb is None:
        return []

    query_emb = torch.Tensor(query_emb)

    document_to_search = docstore.get_all_documents(filters=filters, return_embedding=True)
    # scores = docstore.get_scores(query_emb, document_to_search)

    image_embeds = torch.stack([torch.Tensor(doc.embedding) for doc in document_to_search])
    image_embeds = image_embeds / image_embeds.norm(p=2, dim=-1, keepdim=True)
    query_emb = query_emb / query_emb.norm(p=2, dim=-1, keepdim=True)

    # cosine similarity as logits
    logit_scale = logit_scale.exp()
    logits_per_text = torch.matmul(torch.Tensor(query_emb), image_embeds.t()) * logit_scale
    scores = logits_per_text.T

    candidate_docs = []
    for doc, score in zip(document_to_search, scores):
        curr_meta = deepcopy(doc.meta)
        new_document = Document(id=doc.id, content=doc.content, meta=curr_meta, embedding=doc.embedding)

        # if scale_score:
        #     score = docstore.scale_to_unit_interval(score, docstore.similarity)
        new_document.score = score
        candidate_docs.append(new_document)

    return sorted(candidate_docs, key=lambda x: x.score if x.score is not None else 0.0, reverse=True)[0:top_k]