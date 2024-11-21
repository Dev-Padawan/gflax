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
# pytype: skip-file
from __future__ import annotations

import dataclasses
import functools
from functools import partial
import typing as tp
from typing import Any

import jax

from flax import errors
from flax.nnx import filterlib, reprlib, tracers
from flax.typing import Missing, PathParts
import jax.tree_util as jtu

A = tp.TypeVar('A')
B = tp.TypeVar('B')
F = tp.TypeVar('F', bound=tp.Callable[..., tp.Any])
V = tp.TypeVar('V', bound='Variable[Any]')
GetValueHook = tp.Callable[['Variable[A]', A], A]
SetValueHook = tp.Callable[['Variable[A]', A], A]
CreateValueHook = tp.Callable[['Variable[A]', A], A]
AxisName = str
AxisIndex = int
AddAxisHook = tp.Callable[[V, AxisIndex, AxisName | None], None]
RemoveAxisHook = tp.Callable[[V, AxisIndex, AxisName | None], None]

VariableTypeCache: dict[str, tp.Type[Variable[tp.Any]]] = {}


@dataclasses.dataclass
class VariableMetadata(tp.Generic[A]):
  raw_value: A
  set_value_hooks: tuple[SetValueHook[A], ...] = ()
  get_value_hooks: tuple[GetValueHook[A], ...] = ()
  create_value_hooks: tuple[CreateValueHook[A], ...] = ()
  add_axis_hooks: tuple[AddAxisHook[Variable[A]], ...] = ()
  remove_axis_hooks: tuple[RemoveAxisHook[Variable[A]], ...] = ()
  metadata: tp.Mapping[str, tp.Any] = dataclasses.field(default_factory=dict)


class Variable(tp.Generic[A], reprlib.Representable):
  """The base class for all ``Variable`` types. Create custom ``Variable``
  types by subclassing this class. Numerous NNX graph functions can filter
  for specific ``Variable`` types, for example, :func:`split`, :func:`state`,
  :func:`pop`, and :func:`State.filter`.

  Example usage::

    >>> from flax import nnx
    >>> import jax, jax.numpy as jnp

    >>> class CustomVariable(nnx.Variable):
    ...   pass

    >>> class Model(nnx.Module):
    ...   def __init__(self, rngs):
    ...     self.linear = nnx.Linear(2, 3, rngs=rngs)
    ...     self.custom_variable = CustomVariable(jnp.ones((1, 3)))
    ...   def __call__(self, x):
    ...     return self.linear(x) + self.custom_variable
    >>> model = Model(rngs=nnx.Rngs(0))

    >>> linear_variables = nnx.state(model, nnx.Param)
    >>> jax.tree.map(jnp.shape, linear_variables)
    State({
      'linear': {
        'bias': VariableState(
          type=Param,
          value=(3,)
        ),
        'kernel': VariableState(
          type=Param,
          value=(2, 3)
        )
      }
    })

    >>> custom_variable = nnx.state(model, CustomVariable)
    >>> jax.tree.map(jnp.shape, custom_variable)
    State({
      'custom_variable': VariableState(
        type=CustomVariable,
        value=(1, 3)
      )
    })

    >>> variables = nnx.state(model)
    >>> jax.tree.map(jnp.shape, variables)
    State({
      'custom_variable': VariableState(
        type=CustomVariable,
        value=(1, 3)
      ),
      'linear': {
        'bias': VariableState(
          type=Param,
          value=(3,)
        ),
        'kernel': VariableState(
          type=Param,
          value=(2, 3)
        )
      }
    })
  """

  raw_value: A
  set_value_hooks: tuple[SetValueHook[A], ...]
  get_value_hooks: tuple[GetValueHook[A], ...]
  create_value_hooks: tuple[CreateValueHook[A], ...]
  add_axis_hooks: tuple[AddAxisHook[Variable[A]], ...]
  remove_axis_hooks: tuple[RemoveAxisHook[Variable[A]], ...]
  _trace_state: tracers.TraceState

  def __init__(
    self,
    value: tp.Union[A, VariableMetadata[A]],
    *,
    set_value_hooks: tp.Union[
      SetValueHook[A], tp.Sequence[SetValueHook[A]]
    ] = (),
    get_value_hooks: tp.Union[
      GetValueHook[A], tp.Sequence[GetValueHook[A]]
    ] = (),
    create_value_hooks: tp.Union[
      CreateValueHook[A], tp.Sequence[CreateValueHook[A]]
    ] = (),
    add_axis_hooks: tp.Union[
      AddAxisHook[Variable[A]], tp.Sequence[AddAxisHook[Variable[A]]]
    ] = (),
    remove_axis_hooks: tp.Union[
      RemoveAxisHook[Variable[A]],
      tp.Sequence[RemoveAxisHook[Variable[A]]],
    ] = (),
    **metadata: tp.Any,
  ):
    vars(self)['_trace_state'] = tracers.TraceState()
    if callable(set_value_hooks):
      set_value_hooks = (set_value_hooks,)
    else:
      set_value_hooks = tuple(set_value_hooks)

    if callable(get_value_hooks):
      get_value_hooks = (get_value_hooks,)
    else:
      get_value_hooks = tuple(get_value_hooks)

    if callable(create_value_hooks):
      create_value_hooks = (create_value_hooks,)
    else:
      create_value_hooks = tuple(create_value_hooks)

    if callable(add_axis_hooks):
      add_axis_hooks = (add_axis_hooks,)
    else:
      add_axis_hooks = tuple(add_axis_hooks)

    if callable(remove_axis_hooks):
      remove_axis_hooks = (remove_axis_hooks,)
    else:
      remove_axis_hooks = tuple(remove_axis_hooks)

    if isinstance(value, VariableMetadata):
      value_metadata = dict(value.metadata)
      if value.set_value_hooks:
        set_value_hooks = set_value_hooks + value.set_value_hooks
      if value.get_value_hooks:
        get_value_hooks = get_value_hooks + value.get_value_hooks
      if value.create_value_hooks:
        create_value_hooks = create_value_hooks + value.create_value_hooks
      if value.add_axis_hooks:
        add_axis_hooks = add_axis_hooks + value.add_axis_hooks
      if value.remove_axis_hooks:
        remove_axis_hooks = remove_axis_hooks + value.remove_axis_hooks

      metadata.update(value_metadata)
      value = tp.cast(A, value.raw_value)

    self.raw_value = value

    if 'on_get_value' in vars(type(self)):
      on_get_value = getattr(type(self), 'on_get_value')
      if on_get_value not in get_value_hooks:
        get_value_hooks = (on_get_value, *get_value_hooks)

    if 'on_set_value' in vars(type(self)):
      on_set_value = getattr(type(self), 'on_set_value')
      if on_set_value not in set_value_hooks:
        set_value_hooks = (on_set_value, *set_value_hooks)

    if 'on_create_value' in vars(type(self)):
      on_create_value = getattr(type(self), 'on_create_value')
      if on_create_value not in create_value_hooks:
        create_value_hooks = (on_create_value, *create_value_hooks)

    if 'on_add_axis' in vars(type(self)):
      on_add_axis = getattr(type(self), 'on_add_axis')
      if on_add_axis not in add_axis_hooks:
        add_axis_hooks = (on_add_axis, *add_axis_hooks)

    if 'on_remove_axis' in vars(type(self)):
      on_remove_axis = getattr(type(self), 'on_remove_axis')
      if on_remove_axis not in remove_axis_hooks:
        remove_axis_hooks = (on_remove_axis, *remove_axis_hooks)

    self.get_value_hooks = get_value_hooks
    self.set_value_hooks = set_value_hooks
    self.create_value_hooks = create_value_hooks
    self.add_axis_hooks = add_axis_hooks
    self.remove_axis_hooks = remove_axis_hooks
    vars(self).update(metadata)

    # run create_value hooks
    self.raw_value = self.create_value(self.raw_value)

  if not tp.TYPE_CHECKING:

    def __setattr__(self, name: str, value: Any) -> None:
      return self._setattr(name, value)

  def _setattr(self, name: str, value: tp.Any):
    if not self._trace_state.is_valid():
      raise errors.TraceContextError(
        f'Cannot mutate {type(self).__name__} from a different trace level'
      )

    object.__setattr__(self, name, value)

  @classmethod
  def state(cls, value: A, **metadata) -> VariableState[A]:
    return cls(value, **metadata).to_state()

  def get_metadata(self):
    metadata = vars(self).copy()
    del metadata['raw_value']
    del metadata['_trace_state']
    return metadata

  def copy_from(self, other: Variable[A]) -> None:
    if type(self) is not type(other):
      raise ValueError(
        f'Cannot copy from incompatible container, '
        f'expected {type(self).__name__}, got {type(other).__name__}'
      )
    if self is other:
      return
    trace_state = self._trace_state
    vars_dict = vars(self)
    other_vars = vars(other).copy()
    del other_vars['_trace_state']
    vars_dict.clear()
    vars_dict.update(other_vars, _trace_state=trace_state)

  def update_from_state(self, variable_state: VariableState[A]):
    trace_state = self._trace_state
    variable_vars = vars(self)
    variable_vars.clear()
    variable_vars.update(
      variable_state.get_metadata(),
      raw_value=variable_state.value,
      _trace_state=trace_state,
    )

  @property
  def value(self) -> A:
    value = self.raw_value
    if self.get_value_hooks:
      for hook in self.get_value_hooks:
        value = hook(self, value)
    return value

  @value.setter
  def value(self, value: A):
    if isinstance(value, Variable):
      raise ValueError(
        'Cannot set value to a Variable, ' 'use `copy_from` method instead'
      )
    if self.set_value_hooks:
      for hook in self.set_value_hooks:
        value = hook(self, value)
    self.raw_value = value

  def create_value(self, value: A):
    for hook in self.create_value_hooks:
      value = hook(self, value)
    return value

  def add_axis(self, axis_index: AxisIndex, axis_name: AxisName | None):
    for hook in self.add_axis_hooks:
      hook(self, axis_index, axis_name)

  def remove_axis(self, axis_index: AxisIndex, axis_name: AxisName | None):
    for hook in self.remove_axis_hooks:
      hook(self, axis_index, axis_name)

  def __eq__(self, other: object) -> bool:
    return type(self) is type(other) and vars(other) == vars(self)

  @tp.overload
  def replace(self, value: B, **kwargs) -> Variable[B]: ...

  @tp.overload
  def replace(self, **kwargs) -> Variable[A]: ...

  def replace(self, value: tp.Any = Missing, **kwargs) -> Variable[tp.Any]:
    if value is not Missing:
      kwargs['raw_value'] = value

    # rename `value` to `raw_value`
    if 'value' in kwargs:
      kwargs['raw_value'] = kwargs.pop('value')

    # return `value` if it is a Variable
    if 'raw_value' in kwargs and isinstance(
      value := kwargs['raw_value'], Variable
    ):
      # remove value from kwargs
      kwargs.pop('raw_value')
      if type(self) is not type(value):
        raise ValueError(
          'Cannot replace value from incompatible container, '
          f'expected {type(self).__name__}, got {type(value).__name__}'
        )
      # if kwargs aren't empty, recursively call replace
      # else return variable value
      if kwargs:
        return value.replace(**kwargs)
      else:
        return value

    # get and update attributes
    attributes = vars(self).copy()
    attributes.update(**kwargs)
    # return new instance with updated attributes
    obj = object.__new__(type(self))
    vars(obj).update(attributes)
    return obj

  @classmethod
  def from_metadata(cls, value: A, attributes: tp.Mapping[str, tp.Any]):
    obj = object.__new__(cls)
    vars(obj).update(
      attributes, raw_value=value, _trace_state=tracers.TraceState()
    )
    return obj

  def copy(self: Variable[A]) -> Variable[A]:
    obj = object.__new__(type(self))
    attributes = vars(self).copy()
    attributes['_trace_state'] = tracers.TraceState()
    vars(obj).update(attributes)
    return obj

  def to_state(self: Variable[A]) -> VariableState[A]:
    metadata = self.get_metadata()
    return VariableState(type(self), self.raw_value, **metadata)

  def __nnx_repr__(self):
    yield reprlib.Object(type=type(self))
    for name, value in vars(self).items():
      if name == 'raw_value':
        name = 'value'
      if name.endswith('_hooks') or name == '_trace_state':
        continue
      yield reprlib.Attr(name, repr(value))

  def __treescope_repr__(self, path, subtree_renderer):
    import treescope  # type: ignore[import-not-found,import-untyped]

    children = {}
    for name, value in vars(self).items():
      if name == 'raw_value':
        name = 'value'
      if name.endswith('_hooks') or name == '_trace_state':
        continue
      children[name] = value
    return treescope.repr_lib.render_object_constructor(
      object_type=type(self),
      attributes=children,
      path=path,
      subtree_renderer=subtree_renderer,
    )

  # hooks API
  if tp.TYPE_CHECKING:

    def on_get_value(self, value: A) -> A: ...

    def on_set_value(self, value: A) -> A: ...

    def on_create_value(self, value: A) -> A: ...

    def on_add_axis(
      self: V, axis_index: AxisIndex, axis_name: AxisName | None
    ) -> V: ...

    def on_remove_axis(
      self: V, axis_index: AxisIndex, axis_name: AxisName | None
    ) -> V: ...

  def __jax_array__(self):
    return self.value

  # pickle support
  def __getstate__(self):
    return vars(self).copy()

  def __setstate__(self, state):
    vars(self).update(state)

  # --------------------------------------------
  # proxy methods
  # --------------------------------------------
  # NOTE: we dont override __setattr__ to avoid cases where
  # you need to set an attribute on the variable instance
  def __getattr__(self, name: str) -> tp.Any:
    vars_self = vars(self)
    if name in vars_self:
      return vars_self[name]
    return getattr(self.value, name)

  def __getitem__(self, key) -> tp.Any:
    return self.value[key]  # type: ignore

  def __setitem__(self, key, value) -> None:
    self.value[key] = value  # type: ignore

  def __call__(self, *args, **kwargs) -> tp.Any:
    return self.value(*args, **kwargs)  # type: ignore

  def __len__(self) -> int:
    return len(self.value)  # type: ignore

  def __iter__(self) -> tp.Iterator:
    return iter(self.value)  # type: ignore

  def __contains__(self, item) -> bool:
    return item in self.value  # type: ignore

  def __add__(self, other) -> A:
    return self.value.__add__(other)  # type: ignore

  def __sub__(self, other) -> A:
    return self.value.__sub__(other)  # type: ignore

  def __mul__(self, other) -> A:
    return self.value.__mul__(other)  # type: ignore

  def __matmul__(self, other) -> A:
    return self.value.__matmul__(other)  # type: ignore

  def __truediv__(self, other) -> A:
    return self.value.__truediv__(other)  # type: ignore

  def __floordiv__(self, other) -> A:
    return self.value.__floordiv__(other)  # type: ignore

  def __mod__(self, other) -> A:
    return self.value.__mod__(other)  # type: ignore

  def __divmod__(self, other) -> A:
    return self.value.__divmod__(other)  # type: ignore

  def __pow__(self, other) -> A:
    return self.value.__pow__(other)  # type: ignore

  def __lshift__(self, other) -> A:
    return self.value.__lshift__(other)  # type: ignore

  def __rshift__(self, other) -> A:
    return self.value.__rshift__(other)  # type: ignore

  def __and__(self, other) -> A:
    return self.value.__and__(other)  # type: ignore

  def __xor__(self, other) -> A:
    return self.value.__xor__(other)  # type: ignore

  def __or__(self, other) -> A:
    return self.value.__or__(other)  # type: ignore

  def __radd__(self, other) -> A:
    return self.value.__radd__(other)  # type: ignore

  def __rsub__(self, other) -> A:
    return self.value.__rsub__(other)  # type: ignore

  def __rmul__(self, other) -> A:
    return self.value.__rmul__(other)  # type: ignore

  def __rmatmul__(self, other) -> A:
    return self.value.__rmatmul__(other)  # type: ignore

  def __rtruediv__(self, other) -> A:
    return self.value.__rtruediv__(other)  # type: ignore

  def __rfloordiv__(self, other) -> A:
    return self.value.__rfloordiv__(other)  # type: ignore

  def __rmod__(self, other) -> A:
    return self.value.__rmod__(other)  # type: ignore

  def __rdivmod__(self, other) -> A:
    return self.value.__rdivmod__(other)  # type: ignore

  def __rpow__(self, other) -> A:
    return self.value.__rpow__(other)  # type: ignore

  def __rlshift__(self, other) -> A:
    return self.value.__rlshift__(other)  # type: ignore

  def __rrshift__(self, other) -> A:
    return self.value.__rrshift__(other)  # type: ignore

  def __rand__(self, other) -> A:
    return self.value.__rand__(other)  # type: ignore

  def __rxor__(self, other) -> A:
    return self.value.__rxor__(other)  # type: ignore

  def __ror__(self, other) -> A:
    return self.value.__ror__(other)  # type: ignore

  def __iadd__(self: V, other) -> V:
    value = self.value
    if hasattr(value, '__iadd__'):
      value.__iadd__(other)
    else:
      self.value = value.__add__(other)
    return self

  def __isub__(self: V, other) -> V:
    value = self.value
    if hasattr(value, '__isub__'):
      value.__isub__(other)
    else:
      self.value = value.__sub__(other)
    return self

  def __imul__(self: V, other) -> V:
    value = self.value
    if hasattr(value, '__imul__'):
      value.__imul__(other)
    else:
      self.value = value.__mul__(other)
    return self

  def __imatmul__(self: V, other) -> V:
    value = self.value
    if hasattr(value, '__imatmul__'):
      value.__imatmul__(other)
    else:
      self.value = value.__matmul__(other)
    return self

  def __itruediv__(self: V, other) -> V:
    value = self.value
    if hasattr(value, '__itruediv__'):
      value.__itruediv__(other)
    else:
      self.value = value.__truediv__(other)
    return self

  def __ifloordiv__(self: V, other) -> V:
    value = self.value
    if hasattr(value, '__ifloordiv__'):
      value.__ifloordiv__(other)
    else:
      self.value = value.__floordiv__(other)
    return self

  def __imod__(self: V, other) -> V:
    value = self.value
    if hasattr(value, '__imod__'):
      value.__imod__(other)
    else:
      self.value = value.__mod__(other)
    return self

  def __ipow__(self: V, other) -> V:
    value = self.value
    if hasattr(value, '__ipow__'):
      value.__ipow__(other)
    else:
      self.value = value.__pow__(other)
    return self

  def __ilshift__(self: V, other) -> V:
    value = self.value
    if hasattr(value, '__ilshift__'):
      value.__ilshift__(other)
    else:
      self.value = value.__lshift__(other)
    return self

  def __irshift__(self: V, other) -> V:
    value = self.value
    if hasattr(value, '__irshift__'):
      value.__irshift__(other)
    else:
      self.value = value.__rshift__(other)
    return self

  def __iand__(self: V, other) -> V:
    value = self.value
    if hasattr(value, '__iand__'):
      value.__iand__(other)
    else:
      self.value = value.__and__(other)
    return self

  def __ixor__(self: V, other) -> V:
    value = self.value
    if hasattr(value, '__ixor__'):
      value.__ixor__(other)
    else:
      self.value = value.__xor__(other)
    return self

  def __ior__(self: V, other) -> V:
    value = self.value
    if hasattr(value, '__ior__'):
      value.__ior__(other)
    else:
      self.value = value.__or__(other)
    return self

  def __neg__(self) -> A:
    return self.value.__neg__()  # type: ignore

  def __pos__(self) -> A:
    return self.value.__pos__()  # type: ignore

  def __abs__(self) -> A:
    return self.value.__abs__()  # type: ignore

  def __invert__(self) -> A:
    return self.value.__invert__()  # type: ignore

  def __complex__(self) -> A:
    return self.value.__complex__()  # type: ignore

  def __int__(self) -> A:
    return self.value.__int__()  # type: ignore

  def __float__(self) -> A:
    return self.value.__float__()  # type: ignore

  def __index__(self) -> A:
    return self.value.__index__()  # type: ignore

  def __round__(self, ndigits: int) -> A:
    return self.value.__round__(ndigits)  # type: ignore

  def __trunc__(self) -> A:
    return self.value.__trunc__()  # type: ignore

  def __floor__(self) -> A:
    return self.value.__floor__()  # type: ignore

  def __ceil__(self) -> A:
    return self.value.__ceil__()  # type: ignore


class Param(Variable[A]):
  """The canonical learnable parameter. All learnable parameters
  in NNX layer modules will have the ``Param`` :class:`Variable`
  type::

    >>> from flax import nnx
    >>> import jax, jax.numpy as jnp

    >>> layer = nnx.Linear(2, 3, rngs=nnx.Rngs(0))
    >>> jax.tree.map(jnp.shape, nnx.state(layer))
    State({
      'bias': VariableState(
        type=Param,
        value=(3,)
      ),
      'kernel': VariableState(
        type=Param,
        value=(2, 3)
      )
    })
  """

  pass


class BatchStat(Variable[A]):
  """The mean and variance batch statistics stored in
  the :class:`BatchNorm` layer. Note, these are not the
  learnable scale and bias parameters, but rather the
  running average statistics that are typically used
  during post-training inference::

    >>> from flax import nnx
    >>> import jax, jax.numpy as jnp

    >>> layer = nnx.BatchNorm(3, rngs=nnx.Rngs(0))
    >>> jax.tree.map(jnp.shape, nnx.state(layer))
    State({
      'bias': VariableState(
        type=Param,
        value=(3,)
      ),
      'mean': VariableState(
        type=BatchStat,
        value=(3,)
      ),
      'scale': VariableState(
        type=Param,
        value=(3,)
      ),
      'var': VariableState(
        type=BatchStat,
        value=(3,)
      )
    })
  """

  pass


class Cache(Variable[A]):
  """Autoregressive cache in :class:`MultiHeadAttention`::

  >>> from flax import nnx
  >>> import jax, jax.numpy as jnp

  >>> layer = nnx.MultiHeadAttention(
  ...   num_heads=2,
  ...   in_features=3,
  ...   qkv_features=6,
  ...   out_features=6,
  ...   decode=True,
  ...   rngs=nnx.Rngs(0),
  ... )

  >>> layer.init_cache((1, 3))
  >>> jax.tree.map(jnp.shape, nnx.state(layer, nnx.Cache))
  State({
    'cache_index': VariableState(
      type=Cache,
      value=()
    ),
    'cached_key': VariableState(
      type=Cache,
      value=(1, 2, 3)
    ),
    'cached_value': VariableState(
      type=Cache,
      value=(1, 2, 3)
    )
  })
  """

  pass


class Intermediate(Variable[A]):
  """:class:`Variable` type that is typically used for
  :func:`Module.sow`::

    >>> from flax import nnx
    >>> import jax, jax.numpy as jnp

    >>> class Model(nnx.Module):
    ...   def __init__(self, rngs):
    ...     self.linear1 = nnx.Linear(2, 3, rngs=rngs)
    ...     self.linear2 = nnx.Linear(3, 4, rngs=rngs)
    ...   def __call__(self, x):
    ...     x = self.linear1(x)
    ...     self.sow(nnx.Intermediate, 'i', x)
    ...     x = self.linear2(x)
    ...     return x
    >>> model = Model(rngs=nnx.Rngs(0))

    >>> x = jnp.ones((1, 2))
    >>> y = model(x)
    >>> jax.tree.map(jnp.shape, nnx.state(model, nnx.Intermediate))
    State({
      'i': VariableState(
        type=Intermediate,
        value=((1, 3),)
      )
    })
  """

  pass


class VariableState(tp.Generic[A], reprlib.Representable):
  def __init__(
    self,
    type: type[Variable[tp.Any]],
    value: A,
    **metadata,
  ):
    self.type = type
    self.value = value
    vars(self).update(metadata)

  if tp.TYPE_CHECKING:

    def __getattr__(self, name: str) -> None: ...
    def __setattr__(self, name: str, value: Any) -> None: ...
    def __delattr__(self, name: str) -> None: ...

  def __nnx_repr__(self):
    yield reprlib.Object(type=type(self))
    yield reprlib.Attr('type', self.type.__name__)

    for name, value in vars(self).items():
      if name == 'type' or name.endswith('_hooks'):
        continue
      yield reprlib.Attr(name, repr(value))

  def __treescope_repr__(self, path, subtree_renderer):
    import treescope  # type: ignore[import-not-found,import-untyped]

    children = {'type': self.type}
    for name, value in vars(self).items():
      if name == 'type' or name.endswith('_hooks'):
        continue
      children[name] = value
    return treescope.repr_lib.render_object_constructor(
      object_type=type(self),
      attributes=children,
      path=path,
      subtree_renderer=subtree_renderer,
    )

  def replace(self, value: B) -> VariableState[B]:
    return VariableState(self.type, value, **self.get_metadata())

  def to_variable(self) -> Variable[A]:
    # we use object.__new__ to avoid calling __init__ and bypass the
    # __init__ logic which should not be called twice
    metadata = self.get_metadata()
    variables = object.__new__(self.type)
    vars(variables).update(
      metadata, raw_value=self.value, _trace_state=tracers.TraceState()
    )
    return variables

  def copy(self: VariableState[A]) -> VariableState[A]:
    return jax.tree.map(lambda x: x, self)

  def get_metadata(self) -> dict[str, tp.Any]:
    metadata = vars(self).copy()
    del metadata['type']
    del metadata['value']
    return metadata

  def add_axis(self, axis_index: AxisIndex, axis_name: AxisName | None):
    for hook in self.add_axis_hooks:
      hook(self, axis_index, axis_name)

  def remove_axis(self, axis_index: AxisIndex, axis_name: AxisName | None):
    for hook in self.remove_axis_hooks:
      hook(self, axis_index, axis_name)


def _variable_state_flatten(x: VariableState[tp.Any], *, with_keys: bool):
  metadata = tuple(x.get_metadata().items())
  if with_keys:
    node = (jtu.GetAttrKey('value'), x.value)
  else:
    node = x.value

  return (node,), (x.type, metadata)


def _variable_state_unflatten(
  static: tuple[type[Variable[A]], tuple[tuple[str, tp.Any], ...]],
  children: tuple[A],
) -> VariableState[A]:
  return VariableState(
    type=static[0],
    value=children[0],
    **dict(static[1]),
  )


jtu.register_pytree_with_keys(
  VariableState,
  partial(_variable_state_flatten, with_keys=True),  # type: ignore
  _variable_state_unflatten,  # type: ignore
  flatten_func=partial(_variable_state_flatten, with_keys=False),  # type: ignore
)


def with_metadata(
  initializer: F,
  set_value_hooks: tp.Union[SetValueHook[A], tp.Sequence[SetValueHook[A]]] = (),
  get_value_hooks: tp.Union[SetValueHook[A], tp.Sequence[SetValueHook[A]]] = (),
  create_value_hooks: tp.Union[
    CreateValueHook[A], tp.Sequence[CreateValueHook[A]]
  ] = (),
  add_axis_hooks: tp.Union[
    AddAxisHook[Variable[A]], tp.Sequence[AddAxisHook[Variable[A]]]
  ] = (),
  remove_axis_hooks: tp.Union[
    RemoveAxisHook[Variable[A]],
    tp.Sequence[RemoveAxisHook[Variable[A]]],
  ] = (),
  **metadata: tp.Any,
) -> F:
  if set_value_hooks:
    if callable(set_value_hooks):
      set_value_hooks = (set_value_hooks,)
    else:
      set_value_hooks = tuple(set_value_hooks)
  else:
    set_value_hooks = ()

  if get_value_hooks:
    if callable(get_value_hooks):
      get_value_hooks = (get_value_hooks,)
    else:
      get_value_hooks = tuple(get_value_hooks)
  else:
    get_value_hooks = ()

  if create_value_hooks:
    if callable(create_value_hooks):
      create_value_hooks = (create_value_hooks,)
    else:
      create_value_hooks = tuple(create_value_hooks)
  else:
    create_value_hooks = ()

  if add_axis_hooks:
    if callable(add_axis_hooks):
      add_axis_hooks = (add_axis_hooks,)
    else:
      add_axis_hooks = tuple(add_axis_hooks)
  else:
    add_axis_hooks = ()

  if remove_axis_hooks:
    if callable(remove_axis_hooks):
      remove_axis_hooks = (remove_axis_hooks,)
    else:
      remove_axis_hooks = tuple(remove_axis_hooks)
  else:
    remove_axis_hooks = ()

  @functools.wraps(initializer)
  def wrapper(*args):
    return VariableMetadata(
      initializer(*args),
      set_value_hooks=set_value_hooks,
      get_value_hooks=get_value_hooks,
      create_value_hooks=create_value_hooks,
      add_axis_hooks=add_axis_hooks,
      remove_axis_hooks=remove_axis_hooks,
      metadata=metadata,
    )

  return wrapper  # type: ignore


def split_flat_state(
  flat_state: tp.Iterable[tuple[PathParts, Variable | VariableState]],
  filters: tuple[filterlib.Filter, ...],
) -> tuple[list[tuple[PathParts, Variable | VariableState]], ...]:
  predicates = filterlib.filters_to_predicates(filters)
  # we have n + 1 states, where n is the number of predicates
  # the last state is for values that don't match any predicate
  flat_states: tuple[list[tuple[PathParts, Variable | VariableState]], ...] = (
    tuple([] for _ in predicates)
  )

  for path, value in flat_state:
    for i, predicate in enumerate(predicates):
      if predicate(path, value):
        flat_states[i].append((path, value))
        break
    else:
      raise ValueError(
        'Non-exhaustive filters, got a non-empty remainder: '
        f'{path} -> {value}.'
        '\nUse `...` to match all remaining elements.'
      )

  return flat_states
