# Copyright 2023 The Flax Authors.
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

"""Tests for flax.linen.activation."""

import jax
import jax.numpy as jnp
from absl.testing import absltest, parameterized
from jax import random

from flax import linen as nn

# Parse absl flags test_srcdir and test_tmpdir.
jax.config.parse_flags_with_absl()


class ActivationTest(parameterized.TestCase):
  def test_prelu(self):
    rng = random.key(0)
    x = jnp.ones((4, 6, 5))
    act = nn.PReLU()
    y, _ = act.init_with_output(rng, x)
    self.assertEqual(y.shape, x.shape)

  def test_geglu(self):
    rng = random.key(0)
    x = jnp.ones((4, 6, 5))
    act = nn.GeGLU()
    y, _ = act.init_with_output(rng, x)
    self.assertEqual(y.shape, x.shape)

  def test_geglu_with_dim_expansion(self):
    rng = random.key(0)
    x = jnp.ones((4, 6, 5))
    act = nn.GeGLU(10)
    expected_shape = (4, 6, 10)
    y, _ = act.init_with_output(rng, x)
    self.assertEqual(y.shape, expected_shape)

  def test_geglu_with_dim_contraction(self):
    rng = random.key(0)
    x = jnp.ones((4, 6, 5))
    act = nn.GeGLU(3)
    expected_shape = (4, 6, 3)
    y, _ = act.init_with_output(rng, x)
    self.assertEqual(y.shape, expected_shape)

if __name__ == '__main__':
  absltest.main()
