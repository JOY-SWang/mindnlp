# coding=utf-8
# Copyright 2019-present, Facebook, Inc and the HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# pylint: disable=too-many-instance-attributes
# pylint: disable=C0103
# pylint: disable=W0622
"""
xlm module
"""
import os
import math
import itertools
import inspect
from typing import List,Set,Tuple,Callable, Optional,Dict,Union
import mindspore
import numpy as np
from mindspore import ops,nn,Parameter
from mindspore.common.initializer import Normal, initializer
from ...abc.backbones.pretrained import PretrainedModel
from .xlm_config import XLMConfig

def create_sinusoidal_embeddings(n_pos, dim, out):
    """
    create_sinusoidal_embeddings
    """
    position_enc = np.array(
        [[pos / np.power(10000, 2 * (j // 2) / dim) for j in range(dim)] for pos in range(n_pos)])
    out[:, 0::2] = mindspore.Tensor.float(np.sin(position_enc[:, 0::2]))
    out[:, 1::2] = mindspore.Tensor.float(np.cos(position_enc[:, 1::2]))
    out.detach_()
    out.requires_grad = False


def find_pruneable_heads_and_indices(
    heads: List[int], n_heads: int, head_size: int, already_pruned_heads: Set[int]
) -> Tuple[Set[int], mindspore.Tensor]:
    """
    Finds the heads and their indices taking `already_pruned_heads` into account.

    Args:
        heads (`List[int]`): List of the indices of heads to prune.
        n_heads (`int`): The number of heads in the model.
        head_size (`int`): The size of each head.
        already_pruned_heads (`Set[int]`): A set of already pruned heads.

    Returns:
        `Tuple[Set[int], mindspore.Tensor]`: A tuple with the remaining heads
        and their corresponding indices.
    """
    mask = mindspore.ops.ones(n_heads, head_size)
    heads = set(heads) - already_pruned_heads  # Convert to set and remove already pruned heads
    for head in heads:
        # Compute how many pruned heads are before the head and move the index accordingly
        head = head - sum(1 if h < head else 0 for h in already_pruned_heads)
        mask[head] = 0
    mask = mask.view(-1).contiguous().eq(1)
    index: mindspore.Tensor(dtype = mindspore.int64) = mindspore.numpy.arange(len(mask))[mask].astype(mindspore.int64)
    return heads, index


def get_masks(slen, lengths, causal, padding_mask=None):
    """
    Generate hidden states mask, and optionally an attention mask.
    """
    alen = mindspore.numpy.arange(slen,dtype = mindspore.int64)
    if padding_mask is not None:
        mask = padding_mask
    else:
        assert lengths.max() <= slen
        mask = alen < lengths[:, None]

    # attention mask is the same as mask, or triangular inferior attention (causal)
    bs = lengths.shape[0]
    if causal:
        attn_mask = alen[None, None, :].repeat(bs, slen, 1) <= alen[None, :, None]
    else:
        attn_mask = mask

    # sanity check
    assert mask.shape == (bs, slen)
    assert causal is False or attn_mask.shape == (bs, slen, slen)

    return mask, attn_mask


def prune_linear_layer(layer: nn.Dense, index: mindspore.int64, dim: int = 0) -> nn.Dense:
    """
    Prune a linear layer to keep only entries in index.

    Used to remove heads.

    Args:
        layer (`mindspore.nn.Dense`): The layer to prune.
        index (`Tensor(dtype = mindspore.int64)`): The indices to keep in the layer.
        dim (`int`, *optional*, defaults to 0): The dimension on which to keep the indices.

    Returns:
        `mindspore.nn.Dense`: The pruned layer as a new layer with `requires_grad=True`.
    """
    index = index.to(layer.weight.device)
    W = layer.weight.index_select(dim, index).clone().detach()
    if layer.bias is not None:
        if dim == 1:
            b = layer.bias.clone().detach()
        else:
            b = layer.bias[index].clone().detach()
    new_size = list(layer.weight.size())
    new_size[dim] = len(index)
    new_layer = nn.Dense(new_size[1],
                         new_size[0],
                         has_bias=layer.bias is not None).to(layer.weight.device)
    new_layer.weight.requires_grad = False
    new_layer.weight.copy_(W.contiguous())
    new_layer.weight.requires_grad = True
    if layer.bias is not None:
        new_layer.bias.requires_grad = False
        new_layer.bias.copy_(b.contiguous())
        new_layer.bias.requires_grad = True
    return new_layer


def apply_chunking_to_forward(
    forward_fn: Callable[..., mindspore.Tensor], chunk_size: int, chunk_dim: int, *input_tensors
) -> mindspore.Tensor:
    """
    This function chunks the `input_tensors` into smaller input tensor parts
    of size `chunk_size` over the dimension
    `chunk_dim`. It then applies a layer `forward_fn` to each chunk independently to save memory.

    If the `forward_fn` is independent across the `chunk_dim` this function will yield
    the same result as directly applying `forward_fn` to `input_tensors`.

    Args:
        forward_fn (`Callable[..., torch.Tensor]`):
            The forward function of the model.
        chunk_size (`int`):
            The chunk size of a chunked tensor: `num_chunks = len(input_tensors[0]) / chunk_size`.
        chunk_dim (`int`):
            The dimension over which the `input_tensors` should be chunked.
        input_tensors (`Tuple[torch.Tensor]`):
            The input tensors of `forward_fn` which will be chunked

    Returns:
        `torch.Tensor`: A tensor with the same shape as the `forward_fn`
         would have given if applied`.


    Examples:

    ```python
    # rename the usual forward() fn to forward_chunk()
    def forward_chunk(self, hidden_states):
        hidden_states = self.decoder(hidden_states)
        return hidden_states


    # implement a chunked forward function
    def forward(self, hidden_states):
        return apply_chunking_to_forward(self.forward_chunk, self.chunk_size_lm_head
        , self.seq_len_dim, hidden_states)
    ```"""

    assert len(input_tensors) > 0, f"{input_tensors} has to be a tuple/list of tensors"

    # inspect.signature exist since python 3.5 and is a python method
    # -> no problem with backward compatibility
    num_args_in_forward_chunk_fn = len(inspect.signature(forward_fn).parameters)
    if num_args_in_forward_chunk_fn != len(input_tensors):
        raise ValueError(
            f"forward_chunk_fn expects {num_args_in_forward_chunk_fn} arguments, but only {len(input_tensors)} input "
            "tensors are given"
        )

    if chunk_size > 0:
        tensor_shape = input_tensors[0].shape[chunk_dim]
        for input_tensor in input_tensors:
            if input_tensor.shape[chunk_dim] != tensor_shape:
                raise ValueError(
                    f"All input tenors have to be of the same shape: {tensor_shape}, "
                    f"found shape {input_tensor.shape[chunk_dim]}"
                )

        if input_tensors[0].shape[chunk_dim] % chunk_size != 0:
            raise ValueError(
                f"The dimension to be chunked {input_tensors[0].shape[chunk_dim]} has to be a multiple of the chunk "
                f"size {chunk_size}"
            )

        num_chunks = input_tensors[0].shape[chunk_dim] // chunk_size

        # chunk input tensor into tuples
        input_tensors_chunks = tuple(input_tensor.chunk(num_chunks, dim=chunk_dim) for input_tensor in input_tensors)
        # apply forward fn to every tuple
        output_chunks = tuple(forward_fn(*input_tensors_chunk) for input_tensors_chunk in zip(*input_tensors_chunks))
        # concatenate output at same dimension
        return ops.cat(output_chunks, axis=chunk_dim)

    return forward_fn(*input_tensors)


class XLMPreTrainedModel(PretrainedModel):
    """
    An abstract class to handle weights initialization and a simple interface for downloading and loading pretrained
    models.
    """
    # TODO
    def get_input_embeddings(self):
        pass

    #TODO
    def get_position_embeddings(self):
        pass

    #TODO
    def init_model_weights(self):
        pass

    #TODO
    def resize_position_embeddings(self,new_num_position_embeddings: int):
        pass

    #TODO
    def save(self,save_dir: Union[str, os.PathLike]):
        pass

    #TODO
    def set_input_embeddings(self, value: "nn.Cell"):
        pass

    #TODO
    def post_init(self):
        pass

    config_class = XLMConfig
    load_tf_weights = None
    base_model_prefix = "transformer"



    @property
    def dummy_inputs(self):
        """
        dummy_inputs
        """
        inputs_list = mindspore.Tensor([[7, 6, 0, 0, 1], [1, 2, 3, 0, 0], [0, 0, 0, 4, 5]])
        attns_list = mindspore.Tensor([[1, 1, 0, 0, 1], [1, 1, 1, 0, 0], [1, 0, 0, 1, 1]])
        if self.config.use_lang_emb and self.config.n_langs > 1:
            langs_list = mindspore.Tensor([[1, 1, 0, 0, 1], [1, 1, 1, 0, 0], [1, 0, 0, 1, 1]])
        else:
            langs_list = None
        return {"input_ids": inputs_list, "attention_mask": attns_list, "langs": langs_list}

    def _init_weights(self, module):
        """Initialize the weights."""
        if isinstance(module, nn.Embedding):
            if self.config is not None and self.config.embed_init_std is not None:
                initializer(Normal(sigma=self.config.embed_init_std, mean=0),
                            shape=module.weight.shape,
                            dtype=mindspore.float32)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()
        if isinstance(module, nn.Dense):
            if self.config is not None and self.config.init_std is not None:
                initializer(Normal(sigma=self.config.init_std, mean=0),
                                   shape=module.weight.shape,
                                   dtype=mindspore.float32)
                if module.bias is not None:
                    mindspore.common.initializer.Constant(0.0)(module.bias)
        if isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)


class MultiHeadAttention(nn.Cell):
    """
    MultiHeadAttention
    """
    NEW_ID = itertools.count()

    def __init__(self, n_heads, dim, config):
        super().__init__()
        self.layer_id = next(MultiHeadAttention.NEW_ID)
        self.dim = dim
        self.n_heads = n_heads
        self.dropout = config.attention_dropout
        assert self.dim % self.n_heads == 0

        self.q_lin = nn.Dense(dim, dim)
        self.k_lin = nn.Dense(dim, dim)
        self.v_lin = nn.Dense(dim, dim)
        self.out_lin = nn.Dense(dim, dim)
        self.pruned_heads = set()

    def prune_heads(self, heads):
        """
        prune_heads
        """
        attention_head_size = self.dim // self.n_heads
        if len(heads) == 0:
            return
        heads, index = find_pruneable_heads_and_indices(heads, self.n_heads, attention_head_size, self.pruned_heads)
        # Prune linear layers
        self.q_lin = prune_linear_layer(self.q_lin, index)
        self.k_lin = prune_linear_layer(self.k_lin, index)
        self.v_lin = prune_linear_layer(self.v_lin, index)
        self.out_lin = prune_linear_layer(self.out_lin, index, dim=1)
        # Update hyper params
        self.n_heads = self.n_heads - len(heads)
        self.dim = attention_head_size * self.n_heads
        self.pruned_heads = self.pruned_heads.union(heads)

    def construct(self, input, mask, kv=None, cache=None, head_mask=None, output_attentions=False):
        """
        Self-attention (if kv is None) or attention over source sentence (provided by kv).
        """
        # Input is (bs, qlen, dim)
        # Mask is (bs, klen) (non-causal) or (bs, klen, klen)
        bs, qlen, _ = input.shape #bs,qlen,dim
        if kv is None:
            klen = qlen if cache is None else cache["slen"] + qlen
        else:
            klen = kv.size(1)
        # assert dim == self.dim, f'Dimensions do not match: {dim} input vs {self.dim} configured'
        n_heads = self.n_heads
        dim_per_head = self.dim // n_heads
        mask_reshape = (bs, 1, qlen, klen) if mask.dim() == 3 else (bs, 1, 1, klen)

        def shape(x):
            """projection"""
            return x.view(bs, -1, self.n_heads, dim_per_head).transpose(0,2,1,3)

        def unshape(x):
            """compute context"""
            return x.transpose(0,2,1,3).view(bs, -1, self.n_heads * dim_per_head)

        q = shape(self.q_lin(input))  # (bs, n_heads, qlen, dim_per_head)
        if kv is None:
            k = shape(self.k_lin(input))  # (bs, n_heads, qlen, dim_per_head)
            v = shape(self.v_lin(input))  # (bs, n_heads, qlen, dim_per_head)
        elif cache is None or self.layer_id not in cache:
            k = v = kv
            k = shape(self.k_lin(k))  # (bs, n_heads, qlen, dim_per_head)
            v = shape(self.v_lin(v))  # (bs, n_heads, qlen, dim_per_head)

        if cache is not None:
            if self.layer_id in cache:
                if kv is None:
                    k_, v_ = cache[self.layer_id]
                    k = mindspore.ops.cat([k_, k], axis=2)  # (bs, n_heads, klen, dim_per_head)
                    v = mindspore.ops.cat([v_, v], axis=2)  # (bs, n_heads, klen, dim_per_head)
                else:
                    k, v = cache[self.layer_id]
            cache[self.layer_id] = (k, v)

        scores = mindspore.ops.matmul(q, k.transpose(0,1,3,2)) / math.sqrt(dim_per_head)  # (bs, n_heads, qlen, klen)
        mask = (mask == 0).view(mask_reshape).expand_as(scores)  # (bs, n_heads, qlen, klen)
        scores.masked_fill(mask,mindspore.Tensor(
                           np.finfo(mindspore.dtype_to_nptype(scores.dtype)).min))  # (bs, n_heads, qlen, klen)

        weights = ops.softmax(scores.float(), axis=-1).astype(scores.dtype)  # (bs, n_heads, qlen, klen)
        if self.training:
            weights = ops.dropout(weights, p=self.dropout)# (bs, n_heads, qlen, klen)

        # Mask heads if we want to
        if head_mask is not None:
            weights = weights * head_mask

        context = mindspore.ops.matmul(weights, v)  # (bs, n_heads, qlen, dim_per_head)
        context = unshape(context)  # (bs, qlen, dim)

        outputs = (self.out_lin(context),)
        if output_attentions:
            outputs = outputs + (weights,)
        return outputs


class XLMModel(XLMPreTrainedModel):
    """
    XLMMODEL
    """
    _keys_to_ignore_on_load_missing = [r"position_ids"]

    def __init__(self, config):
        super().__init__(config)

        # encoder / decoder, output layer
        self.is_encoder = config.is_encoder
        self.is_decoder = not config.is_encoder
        if self.is_decoder:
            raise NotImplementedError("Currently XLM can only be used as an encoder")
        # self.with_output = with_output
        self.causal = config.causal

        # dictionary / languages
        self.n_langs = config.n_langs
        self.use_lang_emb = config.use_lang_emb
        self.n_words = config.n_words
        self.eos_index = config.eos_index
        self.pad_index = config.pad_index
        # self.dico = dico
        # self.id2lang = config.id2lang
        # self.lang2id = config.lang2id
        # assert len(self.dico) == self.n_words
        # assert len(self.id2lang) == len(self.lang2id) == self.n_langs

        # model parameters
        self.dim = config.emb_dim  # 512 by default
        self.hidden_dim = self.dim * 4  # 2048 by default
        self.n_heads = config.n_heads  # 8 by default
        self.n_layers = config.n_layers
        self.dropout = config.dropout
        self.attention_dropout = config.attention_dropout
        assert self.dim % self.n_heads == 0, "transformer dim must be a multiple of n_heads"

        # embeddings
        self.position_embeddings = nn.Embedding(config.max_position_embeddings, self.dim)
        if config.sinusoidal_embeddings:
            create_sinusoidal_embeddings(config.max_position_embeddings, self.dim, out=self.position_embeddings.weight)
        if config.n_langs > 1 and config.use_lang_emb:
            self.lang_embeddings = nn.Embedding(self.n_langs, self.dim)
        self.embeddings = nn.Embedding(self.n_words, self.dim, padding_idx=self.pad_index)
        self.layer_norm_emb = nn.LayerNorm((self.dim,), epsilon=config.layer_norm_eps)

        # transformer layers
        self.attentions = nn.CellList()
        self.layer_norm1 = nn.CellList()
        self.ffns = nn.CellList()
        self.layer_norm2 = nn.CellList()
        # if self.is_decoder:
        #     self.layer_norm15 = nn.ModuleList()
        #     self.encoder_attn = nn.ModuleList()

        for _ in range(self.n_layers):
            self.attentions.append(MultiHeadAttention(self.n_heads, self.dim, config=config))
            self.layer_norm1.append(nn.LayerNorm((self.dim,), epsilon=config.layer_norm_eps))
            self.ffns.append(TransformerFFN(self.dim, self.hidden_dim, self.dim, config=config))
            self.layer_norm2.append(nn.LayerNorm((self.dim,), epsilon=config.layer_norm_eps))

        if hasattr(config, "pruned_heads"):
            pruned_heads = config.pruned_heads.copy().items()
            config.pruned_heads = {}
            for layer, heads in pruned_heads:
                if self.attentions[int(layer)].n_heads == config.n_heads:
                    self.prune_heads({int(layer): list(map(int, heads))})

        # Initialize weights and apply final processing
        self.post_init()
        self.position_ids = Parameter(ops.BroadcastTo(shape=(1,-1))
                                     (mindspore.ops.arange(config.max_position_embeddings)))

    def get_input_embeddings(self):
        return self.embeddings

    def set_input_embeddings(self, value: "nn.Cell"):
        self.embeddings = value

    def _prune_heads(self, heads_to_prune):
        """
        Prunes heads of the model. heads_to_prune: dict of {layer_num: list of heads to prune in this layer} See base
        class PreTrainedModel
        """
        for layer, heads in heads_to_prune.items():
            self.attentions[layer].prune_heads(heads)


    def _convert_head_mask_to_5d(self, head_mask, num_hidden_layers):
        """-> [num_hidden_layers x batch x num_heads x seq_length x seq_length]"""
        if head_mask.dim() == 1:
            head_mask = head_mask.unsqueeze(0).unsqueeze(0).unsqueeze(-1).unsqueeze(-1)
            head_mask = head_mask.expand(num_hidden_layers, -1, -1, -1, -1)
        elif head_mask.dim() == 2:
            head_mask = head_mask.unsqueeze(1).unsqueeze(-1).unsqueeze(-1)  # We can specify head_mask for each layer
        assert head_mask.dim() == 5, f"head_mask.dim != 5, instead {head_mask.dim()}"
        head_mask = head_mask.to(dtype=self.dtype)  # switch to float if need + fp16 compatibility
        return head_mask


    def get_head_mask(
        self, head_mask: Optional[mindspore.Tensor], num_hidden_layers: int, is_attention_chunked: bool = False
    ) -> mindspore.Tensor:
        """
        Prepare the head mask if needed.
        Args:
            head_mask (`torch.Tensor` with shape `[num_heads]` or `[num_hidden_layers x num_heads]`, *optional*):
                The mask indicating if we should keep the heads or not (1.0 for keep, 0.0 for discard).
            num_hidden_layers (`int`):
                The number of hidden layers in the model.
            is_attention_chunked: (`bool`, *optional*, defaults to `False`):
                Whether or not the attentions scores are computed by chunks or not.

        Returns:
            `torch.Tensor` with shape `[num_hidden_layers x batch x num_heads x seq_length x seq_length]` or list with
            `[None]` for each layer.
        """
        if head_mask is not None:
            head_mask = self._convert_head_mask_to_5d(head_mask, num_hidden_layers)
            if is_attention_chunked is True:
                head_mask = head_mask.unsqueeze(-1)
        else:
            head_mask = [None] * num_hidden_layers

        return head_mask

    def construct(
        self,
        input_ids: Optional[mindspore.Tensor] = None,
        attention_mask: Optional[mindspore.Tensor] = None,
        langs: Optional[mindspore.Tensor] = None,
        token_type_ids: Optional[mindspore.Tensor] = None,
        position_ids: Optional[mindspore.Tensor] = None,
        lengths: Optional[mindspore.Tensor] = None,
        cache: Optional[Dict[str, mindspore.Tensor]] = None,
        head_mask: Optional[mindspore.Tensor] = None,
        inputs_embeds: Optional[mindspore.Tensor] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Tuple:
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if input_ids is not None:
            bs, slen = input_ids.shape
        else:
            bs, slen = inputs_embeds.shape[:-1]

        if lengths is None:
            if input_ids is not None:
                lengths = (input_ids != self.pad_index).sum(axis=1).astype(mindspore.int64)
            else:
                lengths = mindspore.Tensor([slen] * bs)
        # mask = input_ids != self.pad_index

        # check inputs
        assert lengths.shape[0] == bs
        assert mindspore.Tensor.max(lengths) <= slen
        # input_ids = input_ids.transpose(0, 1)  # batch size as dimension 0
        # assert (src_enc is None) == (src_len is None)
        # if src_enc is not None:
        #     assert self.is_decoder
        #     assert src_enc.size(0) == bs

        # generate masks
        mask, attn_mask = get_masks(slen, lengths, self.causal, padding_mask=attention_mask)
        # if self.is_decoder and src_enc is not None:
        #     src_mask = torch.arange(src_len.max(), dtype=torch.long, device=lengths.device) < src_len[:, None]

        # position_ids
        if position_ids is None:
            position_ids = self.position_ids[:, :slen]
        else:
            assert position_ids.size() == (bs, slen)  # (slen, bs)
            # position_ids = position_ids.transpose(0, 1)

        # langs
        if langs is not None:
            assert langs.size() == (bs, slen)  # (slen, bs)
            # langs = langs.transpose(0, 1)

        # Prepare head mask if needed
        head_mask = self.get_head_mask(head_mask, self.config.n_layers)

        # do not recompute cached elements
        if cache is not None and input_ids is not None:
            _slen = slen - cache["slen"]
            input_ids = input_ids[:, -_slen:]
            position_ids = position_ids[:, -_slen:]
            if langs is not None:
                langs = langs[:, -_slen:]
            mask = mask[:, -_slen:]
            attn_mask = attn_mask[:, -_slen:]

        # embeddings
        if inputs_embeds is None:
            inputs_embeds = self.embeddings(input_ids)
        tensor = inputs_embeds + self.position_embeddings(position_ids).expand_as(inputs_embeds)
        if langs is not None and self.use_lang_emb and self.n_langs > 1:
            tensor = tensor + self.lang_embeddings(langs)
        if token_type_ids is not None:
            tensor = tensor + self.embeddings(token_type_ids)
        tensor = self.layer_norm_emb(tensor)
        if self.training:
            tensor = ops.dropout(tensor, p = self.dropout)
        tensor *= mask.unsqueeze(-1).to(tensor.dtype)

        # transformer layers
        hidden_states = () if output_hidden_states else None
        attentions = () if output_attentions else None
        for i in range(self.n_layers):
            if output_hidden_states:
                hidden_states = hidden_states + (tensor,)

            # self attention
            attn_outputs = self.attentions[i](
                tensor,
                attn_mask,
                cache=cache,
                head_mask=head_mask[i],
                output_attentions=output_attentions,
            )
            attn = attn_outputs[0]
            if output_attentions:
                attentions = attentions + (attn_outputs[1],)
            if self.training:
                attn = ops.dropout(attn, p=self.dropout)
            tensor = tensor + attn
            tensor = self.layer_norm1[i](tensor)

            # encoder attention (for decoder only)
            # if self.is_decoder and src_enc is not None:
            #     attn = self.encoder_attn[i](tensor, src_mask, kv=src_enc, cache=cache)
            #     attn = nn.functional.dropout(attn, p=self.dropout, training=self.training)
            #     tensor = tensor + attn
            #     tensor = self.layer_norm15[i](tensor)

            # FFN
            tensor = tensor + self.ffns[i](tensor)
            tensor = self.layer_norm2[i](tensor)
            tensor *= mask.unsqueeze(-1).to(tensor.dtype)

        # Add last hidden state
        if output_hidden_states:
            hidden_states = hidden_states + (tensor,)

        # update cache length
        if cache is not None:
            cache["slen"] += tensor.size(1)

        # move back sequence length to dimension 0
        # tensor = tensor.transpose(0, 1)
        return tuple(v for v in [tensor, hidden_states, attentions] if v is not None)


class TransformerFFN(nn.Cell):
    """
    TransformerFFN
    """
    def __init__(self, in_dim, dim_hidden, out_dim, config):
        super().__init__()
        self.dropout = config.dropout
        self.lin1 = nn.Dense(in_dim, dim_hidden)
        self.lin2 = nn.Dense(dim_hidden, out_dim)
        self.act = nn.GELU() if config.gelu_activation else nn.ReLU()
        self.chunk_size_feed_forward = config.chunk_size_feed_forward
        self.seq_len_dim = 1

    def construct(self, input):
        return apply_chunking_to_forward(self.ff_chunk, self.chunk_size_feed_forward, self.seq_len_dim, input)

    def ff_chunk(self, input):
        """
        ff_chunk
        """
        x = self.lin1(input)
        x = self.act(x)
        x = self.lin2(x)
        if self.training:
            x = ops.dropout(x, p=self.dropout)
        return x


class XLMPredLayer(nn.Cell):
    """
    Prediction layer (cross_entropy or adaptive_softmax).
    """
    def __init__(self, config):
        super().__init__()
        self.asm = config.asm
        self.n_words = config.n_words
        self.pad_index = config.pad_index
        dim = config.emb_dim

        if config.asm is False:
            self.proj = nn.Dense(dim, config.n_words, has_bias=True)
        ## else :TO DO nn.AdaptiveLogSoftmaxWithLoss

    def construct(self, x, y=None):
        """Compute the loss, and optionally the scores."""
        outputs = ()
        if self.asm is False:
            scores = self.proj(x)
            outputs = (scores,) + outputs
            if y is not None:
                loss = ops.cross_entropy(scores.view(-1, self.n_words),
                                        y.view(-1),
                                        reduction="mean")
                outputs = (loss,) + outputs
        else:
            scores = self.proj.log_prob(x)
            outputs = (scores,) + outputs
            if y is not None:
                _, loss = self.proj(x, y)
                outputs = (loss,) + outputs

        return outputs
