# Copyright 2024 The Flax Authors.
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

import functools
import typing as tp

import jax
from jax.interpreters import pxla
from jax.sharding import PartitionSpec

from flax.nnx import variables
from flax.typing import (
  Array,
  ArrayPytree,  # pylint: disable=invalid-name
  PartitionSpecPytree,  # pylint: disable=invalid-name
  Sharding,
)
from flax import errors

A = tp.TypeVar('A')
F = tp.TypeVar('F', bound=tp.Callable[..., tp.Any])
PARTITION_NAME = 'partition_name'


def add_axis(tree: A, index: int, params: tp.Mapping[tp.Any, tp.Any]) -> A:
  axis_name = _get_partition_name(params)

  def _add_axis(x: tp.Any):
    if isinstance(x, variables.VariableState):
      if hasattr(x, 'sharding') and x.sharding is not None:
        if axis_name is None:
          raise errors.AxisNameMissingError(x.sharding)
        sharding: list[str | None] = list(x.sharding)
        while len(sharding) < index:
          sharding.append(None)
        sharding.insert(index, axis_name)
        x.sharding = tuple(sharding)  # type: ignore

      x.add_axis(index, axis_name)
    return x

  return jax.tree.map(
    _add_axis, tree, is_leaf=lambda x: isinstance(x, variables.VariableState)
  )


def remove_axis(tree: A, index: int, params: tp.Mapping[tp.Any, tp.Any]) -> A:
  axis_name = _get_partition_name(params)

  def _remove_axis(x: tp.Any):
    if isinstance(x, variables.VariableState):
      if hasattr(x, 'sharding') and x.sharding is not None:
        if axis_name is None:
          raise errors.AxisNameMissingError(x.sharding)
        sharding = list(x.sharding)
        assert sharding.pop(index) == axis_name
        x.sharding = tuple(sharding)
      x.remove_axis(index, axis_name)
    return x

  return jax.tree.map(
    _remove_axis,
    tree,
    is_leaf=lambda x: isinstance(x, variables.VariableState),
  )


def _get_partition_name(params: tp.Mapping[tp.Any, tp.Any]) -> str | None:
  if PARTITION_NAME not in params:
    return None
  return params[PARTITION_NAME]


def get_partition_spec(tree: A) -> A:
  """Extracts a PartitionSpec tree from a PyTree containing ``Variable`` values."""

  def _maybe_replicate(x):
    if hasattr(x, 'shape'):
      return PartitionSpec()
    else:
      return None

  def from_rules(sharding, sharding_rules):
    rules = {alias: on_mesh for (alias, on_mesh) in sharding_rules}
    return (rules[s] if s in rules else s for s in sharding)

  def f(x):
    if isinstance(x, (variables.VariableState, variables.Variable)):
      if hasattr(x, 'sharding') and x.sharding:
        if hasattr(x, 'sharding_rules') and x.sharding_rules:
          return x.replace(PartitionSpec(*from_rules(x.sharding, x.sharding_rules)))
        return x.replace(PartitionSpec(*x.sharding))
      else:
        return x.replace(_maybe_replicate(x.value))

    return _maybe_replicate(x)

  return jax.tree.map(
    f, tree, is_leaf=lambda x: isinstance(x, variables.VariableState)
  )


def get_named_sharding(tree: A, mesh: jax.sharding.Mesh) -> A:
  spec = get_partition_spec(tree)
  sharding = jax.tree.map(
    lambda p: jax.sharding.NamedSharding(mesh, p), spec
  )
  return sharding


# Dynamic Axis Mapping Rngs
# ------------------------------------------------------------------------------


def _global_mesh_defined() -> bool:
  """Checks if global mesh resource environment is defined."""
  env = pxla.thread_resources.env
  return env.physical_mesh.devices.shape != ()  # pylint: disable=g-explicit-bool-comparison


def _with_sharding_constraint(
  x: Array,
  axis_resources: tp.Optional[jax.sharding.PartitionSpec],
  mesh: tp.Optional[jax.sharding.Mesh] = None,
):
  # if jax.devices()[0].platform == "cpu" or (
  if not _global_mesh_defined() and mesh is None:
    return x
  else:
    if mesh is not None and axis_resources is not None:
      sharding = jax.sharding.NamedSharding(mesh, axis_resources)
      return jax.lax.with_sharding_constraint(x, sharding)
    return jax.lax.with_sharding_constraint(x, axis_resources)


def _is_spec(x):
  return x is None or (
    isinstance(x, tuple) and all(isinstance(e, str) or e is None for e in x)
  )


def with_sharding_constraint(
  x: ArrayPytree,
  axis_resources: PartitionSpecPytree,
  mesh: tp.Optional[jax.sharding.Mesh] = None,
):
  # If no axis binding is set, this is a no-op.
  if axis_resources is None:
    return x
  # Translate logical names to mesh assignments.
  return jax.tree.map(
    functools.partial(_with_sharding_constraint, mesh=mesh),
    x,
    axis_resources,
    is_leaf=_is_spec,
  )


def with_partitioning(
  initializer: F,
  sharding: Sharding,
  mesh: tp.Optional[jax.sharding.Mesh] = None,
  **metadata: tp.Any,
) -> F:
  return variables.with_metadata(
    initializer,
    sharding=sharding,
    mesh=mesh,
    **metadata,
  )
