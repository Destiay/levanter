import tempfile

import jax.numpy as jnp
import numpy as np
import pytest
from jax.random import PRNGKey

import haliax

from levanter.compat.hf_checkpoints import _convert_to_jnp
from levanter.models.attention import AttentionMask
from levanter.models.backpack import BackpackConfig, BackpackLMHeadModel
from levanter.models.gpt2 import Gpt2Config, Gpt2LMHeadModel
from levanter.utils.tree_utils import inference_mode
from test_utils import skip_if_no_torch


@skip_if_no_torch
def test_save_backpack_model_with_code():
    import torch

    converter = BackpackConfig.default_hf_checkpoint_converter
    tokenizer = converter.tokenizer
    cls = converter.HFAutoModelClass()
    config = converter.HfConfigClass(
        n_embd=32,
        n_positions=512,
        n_head=8,
        n_layer=2,
        vocab_size=len(tokenizer),
        resid_pdrop=0.0,
    )

    model = cls(config)
    model.eval()

    with tempfile.TemporaryDirectory() as tmpdir:
        lev_config = converter.config_from_hf_config(config)
        model.save_pretrained(tmpdir)
        loaded_checkpoint = converter.load_state_dict(tmpdir)

    roundtrip_hf_config = converter.hf_config_from_config(lev_config)

    for k, v in roundtrip_hf_config.__dict__.items():
        assert getattr(roundtrip_hf_config, k) == v, f"{k} {getattr(roundtrip_hf_config, k)} != {v}"

    Vocab = converter.Vocab
    lev_model = BackpackLMHeadModel.init(Vocab, lev_config, key=PRNGKey(0))
    lev_model = lev_model.from_state_dict(loaded_checkpoint)
    lev_model = inference_mode(lev_model, True)

    with tempfile.TemporaryDirectory() as tmpdir:
        converter._save_pretrained_local(
            lev_model, tmpdir, save_tokenizer=True, save_reference_code=True, max_shard_size=1e8
        )

        new_converter = converter.replaced(reference_checkpoint=tmpdir, trust_remote_code=True)

        assert new_converter.config_from_hf_config(config) == lev_config
        loaded_model = new_converter.load_pretrained(BackpackLMHeadModel)
        loaded_model = inference_mode(loaded_model, True)

        assert loaded_model.config == lev_model.config
        assert loaded_model.Vocab == lev_model.Vocab

        input = haliax.random.randint(PRNGKey(0), lev_model.config.Pos, 0, lev_model.Vocab.size)
        causal_mask = AttentionMask.causal()
        np.testing.assert_equal(
            np.array(lev_model(input, causal_mask, key=None).array),
            np.array(loaded_model(input, causal_mask, key=None).array),
        )

        # now double check that the pytorch model is the same
        loaded_model = cls.from_pretrained(tmpdir)
        torch_input = torch.from_numpy(np.array(input.array)).to(torch.int64).unsqueeze(0)
        loaded_model.eval()
        np.testing.assert_allclose(
            model(torch_input).logits[0].detach().numpy(), loaded_model(torch_input).logits[0].detach().numpy()
        )


@skip_if_no_torch
def test_conversion_to_jnp_bfloat16():
    import torch

    x = torch.arange(10, dtype=torch.bfloat16) / 3.14
    with pytest.raises(TypeError):
        x.cpu().numpy()

    x_jnp = _convert_to_jnp(x)
    assert x_jnp.dtype == jnp.bfloat16
    assert x_jnp.shape == x.shape
    assert jnp.allclose(x_jnp, jnp.arange(10, dtype=jnp.bfloat16) / 3.14)


def test_save_sharded_checkpoints():
    converter = Gpt2Config.default_hf_checkpoint_converter

    nano_config = Gpt2Config(hidden_dim=64, num_heads=2, num_layers=2, resid_pdrop=0.0, use_flash_attention=False)

    nano_model = Gpt2LMHeadModel.init(converter.Vocab, nano_config, key=PRNGKey(3))

    with tempfile.TemporaryDirectory() as tmpdir:
        converter.save_pretrained(nano_model, tmpdir, max_shard_size=1024)

        # make sure we saved a few different files
        import glob

        assert len(glob.glob(tmpdir + "/*.safetensors")) > 1

        loaded_model = converter.load_pretrained(nano_model.config, ref=tmpdir)

        assert loaded_model.config == nano_model.config
        assert loaded_model.Vocab == nano_model.Vocab

        input = haliax.random.randint(PRNGKey(0), nano_model.config.Pos, 0, nano_model.Vocab.size)
        causal_mask = AttentionMask.causal()
        np.testing.assert_allclose(
            np.array(nano_model(input, causal_mask, key=None).array),
            np.array(loaded_model(input, causal_mask, key=None).array),
            rtol=1e-6,
            atol=1e-6,
        )
