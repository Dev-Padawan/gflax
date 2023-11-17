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

import typing as tp

import jax
from jax.nn.initializers import constant
from jax.nn.initializers import delta_orthogonal
from jax.nn.initializers import glorot_normal
from jax.nn.initializers import glorot_uniform
from jax.nn.initializers import he_normal
from jax.nn.initializers import he_uniform
from jax.nn.initializers import kaiming_normal
from jax.nn.initializers import kaiming_uniform
from jax.nn.initializers import lecun_normal
from jax.nn.initializers import lecun_uniform
from jax.nn.initializers import normal
from jax.nn.initializers import orthogonal
from jax.nn.initializers import uniform
from jax.nn.initializers import variance_scaling
from jax.nn.initializers import xavier_normal
from jax.nn.initializers import xavier_uniform
import jax.numpy as jnp

Shape = tp.Sequence[int]
DTypeLikeInexact = tp.Any
Array = jax.Array


class Initializer(tp.Protocol):
  @staticmethod
  def __call__(
    key: Array, shape: Shape, dtype: DTypeLikeInexact = jnp.float_
  ) -> Array:
    ...


def zeros() -> Initializer:
  """Builds an initializer that returns a constant array full of zeros.

  >>> import jax, jax.numpy as jnp
  >>> from flax.linen.initializers import zeros_init
  >>> zeros_initializer = zeros_init()
  >>> zeros_initializer(jax.random.key(42), (2, 3), jnp.float32)
  Array([[0., 0., 0.],
         [0., 0., 0.]], dtype=float32)
  """
  return jax.nn.initializers.zeros


def ones() -> Initializer:
  """Builds an initializer that returns a constant array full of ones.

  >>> import jax, jax.numpy as jnp
  >>> from flax.linen.initializers import ones_init
  >>> ones_initializer = ones_init()
  >>> ones_initializer(jax.random.key(42), (3, 2), jnp.float32)
  Array([[1., 1.],
         [1., 1.],
         [1., 1.]], dtype=float32)
  """
  return jax.nn.initializers.ones
