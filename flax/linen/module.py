# Copyright 2022 The Flax Authors.
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

"""Flax Module."""
import contextlib
import dataclasses
import enum
import functools
import inspect
import re
import threading
import typing
import weakref
from typing import (Any, Callable, Dict, Iterable, List, Sequence, NamedTuple, Mapping,
                    Optional, Set, Tuple, Type, TypeVar, Union, overload)

import jax
import numpy as np
import jax.numpy as jnp
from typing_extensions import Protocol, \
  dataclass_transform  # pytype: disable=not-supported-yet

import flax
from flax import (config, core, errors, serialization, traceback_util,
                  traverse_util)
from flax.core import Scope
from flax.core.frozen_dict import FrozenDict
from flax.core.scope import (  # pylint: disable=g-multiple-import
    CollectionFilter, DenyList, FrozenVariableDict, Variable, VariableDict,
    union_filters)
from flax.ids import FlaxId
from flax.ids import uuid
from flax.linen import kw_only_dataclasses


traceback_util.register_exclusion(__file__)

PRNGKey = Any  # pylint: disable=invalid-name
RNGSequences = Dict[str, PRNGKey]
Array = Any    # pylint: disable=invalid-name


T = TypeVar('T')
K = TypeVar('K')
M = TypeVar('M', bound='Module')
_CallableT = TypeVar('_CallableT', bound=Callable)


# Used for abstractly testing module behavior.
TestScope = type('TestScope',
                 (Scope,),
                 {'make_rng': lambda self, name: jax.random.PRNGKey(0)})


# pylint: disable=protected-access,attribute-defined-outside-init

def _indent(x: str, num_spaces: int):
  indent_str = ' ' * num_spaces
  lines = x.split('\n')
  # skip last line because it is always empty and should not be indented.
  assert not lines[-1]
  return '\n'.join(indent_str + line for line in lines[:-1]) + '\n'


def _attr_repr(value: Any):
  if callable(value) and getattr(value, '__name__', None):
    value_rep = value.__name__
  else:
    value_rep = repr(value)
  return value_rep


def _module_repr(module: 'Module', num_spaces: int = 4):
  """Returns a pretty printed representation of the module."""
  cls = type(module)
  cls_name = cls.__name__
  rep = ''

  attributes = {f.name: f.type for f in dataclasses.fields(cls)
                if f.name not in ('parent', 'name')}
  child_modules = {k: v for k, v in module._state.children.items()  # pytype: disable=attribute-error
                   if isinstance(v, Module)}
  if attributes:
    rep += '# attributes\n'
    for attr in attributes.keys():
      # TODO(jheek): can we get a nice string representation of attribute types?
      value = getattr(module, attr, None)
      value_rep = _attr_repr(value)
      rep += f'{attr} = {value_rep}\n'
  if child_modules:
    rep += '# children\n'
    for name, child in child_modules.items():
      child_rep = _module_repr(child, num_spaces)
      rep += f'{name} = {child_rep}\n'
  if rep:
    return f'{cls_name}(\n{_indent(rep, num_spaces)})'
  else:
    return f'{cls_name}()'

# Tabulation utilities.
# -----------------------------------------------------------------------------

_find_non_lifted_module = re.compile(r'.*\((.*)\)')

def _fix_path_part(part: str):
  """Fixes a path part by removing transformation name and parenthesis sometimes
  inserted by lifted transformations"""
  match = _find_non_lifted_module.match(part)
  if match:
    return match.group(1)
  return part

@dataclasses.dataclass
class _CallInfo:
  index: int
  path: Tuple[str, ...]
  module_type: Type['Module']
  method: str
  args: Tuple[Any, ...]
  kwargs: Dict[str, Any]
  outputs: Any

@dataclasses.dataclass
class _CallInfoContext(threading.local):
  index: int
  calls: List[_CallInfo]

  def get_call_index(self, module: 'Module') -> int:
    index = self.index
    self.index += 1
    return index

@contextlib.contextmanager
def _tabulate_context():
  _context.call_info_stack.append(_CallInfoContext(0, []))
  try:
    yield
  finally:
    _context.call_info_stack.pop()

# Track parent relationship across Modules.
# -----------------------------------------------------------------------------
class _DynamicContext(threading.local):
  """Dynamic context."""
  # TODO(marcvanzee): switch to using contextvars once minimum python version is
  # 3.7

  def __init__(self):
    self.module_stack = [None,]
    self.capture_stack = []
    self.call_info_stack = []

# The global context
_context = _DynamicContext()


class _Sentinel:

  def __copy__(self):
    return self  # Do not copy singleton sentinel.

  def __deepcopy__(self, memo):
    del memo
    return self  # Do not copy singleton sentinel.


_unspecified_parent = _Sentinel()


# Enable automatic named_call wrapping for labelling profile traces.
# -----------------------------------------------------------------------------
_use_named_call = config.flax_profile


def _derive_profiling_name(module, fn):
  def _get_fn_name(fn):
    if isinstance(fn, functools.partial):
      return _get_fn_name(fn.func)
    return fn.__name__
  fn_name = _get_fn_name(fn)
  method_suffix = f'.{fn_name}' if fn_name != '__call__' else ''
  module_name = module.name or module.__class__.__name__
  return f'{module_name}{method_suffix}'


def enable_named_call():
  """Enables named call wrapping for labelling profile traces.

  When named call wrapping is enabled all JAX ops executed in a Module
  will be run under ``jax.named_scope``. The ``Module`` class name will
  show up around the operations belonging to that Module in the
  Tensorboard profiling UI, simplifying the profiling process.

  Note that ``jax.named_scope`` only works for
  compiled functions (e.g.: using jax.jit or jax.pmap).
  """
  global _use_named_call
  _use_named_call = True


def disable_named_call():
  """Disables named call wrapping.

  See ``enable_named_call``
  """
  global _use_named_call
  _use_named_call = False


@contextlib.contextmanager
def override_named_call(enable: bool = True):
  # pylint: disable=g-doc-return-or-yield
  """Returns a context manager that enables/disables named call wrapping.

  Args:
    enable: If true, enables named call wrapping for labelling profile traces.
      (see ``enabled_named_call``).
  """
  # pylint: enable=g-doc-return-or-yield
  global _use_named_call
  use_named_call_prev = _use_named_call
  _use_named_call = enable
  try:
    yield
  finally:
    _use_named_call = use_named_call_prev


# Utilities for pytrees of Modules defined inside setup()
# -----------------------------------------------------------------------------


def _sorted_items(x):
  """Returns items of a dict ordered by keys."""
  return sorted(x.items(), key=lambda x: x[0])


def _get_suffix_value_pairs(
    tree_or_leaf: Any) -> List[Tuple[str, Type['Module']]]:
  """Helper for naming pytrees of submodules."""
  dict_or_leaf = serialization.to_state_dict(tree_or_leaf)
  if not isinstance(dict_or_leaf, dict) or not dict_or_leaf:
    return [('', tree_or_leaf)]
  else:
    flat_dict = traverse_util.flatten_dict(dict_or_leaf)
    return [('_' + '_'.join(k), v) for k, v in _sorted_items(flat_dict)]


def _map_over_modules_in_tree(fn, tree_or_leaf):
  """Helper for mapping function over submodules."""
  dict_or_leaf = serialization.to_state_dict(tree_or_leaf)
  if not isinstance(dict_or_leaf, dict) or not dict_or_leaf:
    return fn('', tree_or_leaf)
  else:
    flat_dict = traverse_util.flatten_dict(dict_or_leaf, keep_empty_nodes=True)
    mapped_flat_dict = {k: fn('_' + '_'.join(k), v)
                        for k, v in _sorted_items(flat_dict)}
    return serialization.from_state_dict(
        tree_or_leaf, traverse_util.unflatten_dict(mapped_flat_dict))


def _all_names_on_object(obj: Any) -> Set[str]:
  """Gets all names of attributes on `obj` and its classes throughout MRO.

  Args:
    obj: The object to get names for.
  Returns:
    A set of names of attributes of `obj` and its classes.
  """
  nameset = set(obj.__dict__.keys())
  for cls in obj.__class__.__mro__:
    nameset = nameset.union(set(cls.__dict__.keys()))
  return nameset


def _freeze_attr(val: Any) -> Any:
  """Recursively wrap the given attribute `var` in ``FrozenDict``."""
  if isinstance(val, (dict, FrozenDict)):
    return FrozenDict({k: _freeze_attr(v) for k, v in val.items()})
  elif isinstance(val, tuple):
    # Special case namedtuples and special JAX tuple structures otherwise they
    # would be downgraded to normal tuples.
    if hasattr(val, '_fields') or type(val).__name__ == 'PartitionSpec':
      return type(val)(*[_freeze_attr(v) for v in val])
    else:
      return tuple(_freeze_attr(v) for v in val)
  elif isinstance(val, list):
    return tuple(_freeze_attr(v) for v in val)
  else:
    return val


# Method wrapping of "compact methods" and setup()
# -----------------------------------------------------------------------------
def compact(fun: _CallableT) -> _CallableT:
  """Marks the given module method allowing inlined submodules.

  Methods wrapped in @compact can define submodules directly within the method.

  For instance::

    @compact
    __call__(self, x, features):
      x = nn.Dense(features)(x)
      ...

  At most one method in each Module may be wrapped with @compact.

  Args:
    fun: The Module method to mark as compact.
  Returns:
    The given function `fun` marked as compact.
  """
  fun.compact = True  # type: ignore[attr-defined]
  return fun


def nowrap(fun: _CallableT) -> _CallableT:
  """Marks the given module method as a helper method that needn't be wrapped.

  Methods wrapped in @nowrap are private helper methods that needn't be wrapped
  with the state handler or a separate named_call transform.

  This is needed in several concrete instances:
   - if you're subclassing a method like Module.param and don't want this
     overriden core function decorated with the state management wrapper.
   - If you want a method to be callable from an unbound Module (e.g.: a
     function of construction of arguments that doesn't depend on params/RNGs)

  For instance::

    @nowrap
    def _make_dense(self, num_features):
      return nn.Dense(num_features)

    @compact
    def __call__(self, x):
      # now safe to use constructor helper even if using named_call
      dense = self._make_dense(self.num_features)
      return dense(x)

  Args:
    fun: The Module method to mark as nowrap.
  Returns:
    The given function `fun` marked as nowrap.
  """
  fun.nowrap = True  # type: ignore[attr-defined]
  return fun


def _get_local_method_names(cls: Any,
                            exclude: Iterable[str] = ()) -> Tuple[str, ...]:
  """Gets method names of a class, excluding class and static methods.

  Args:
    cls: The class to get method names for.
    exclude: Names to exclude from output.
  Returns:
    A list of method names.
  """
  true_methods = set()
  for m in cls.__dict__:
    if callable(cls.__dict__[m]) and not inspect.isclass(cls.__dict__[m]):  # pytype: disable=not-supported-yet
      mtype = type(cls.__dict__[m])
      if mtype != staticmethod and mtype != classmethod:
        true_methods.add(m)
  return tuple(true_methods.difference(set(exclude)))

def _get_local_descriptor_names(cls: Any,
                                exclude: Iterable[str] = ()) -> Tuple[str, ...]:
  """Gets descriptor names of a class.

  Args:
    cls: The class to get property names for.
    exclude: Names to exclude from output.
  Returns:
    A list of property names.
  """
  true_properties = set()
  for m, attr in cls.__dict__.items():
    if not callable(attr) and (
      hasattr(attr, '__get__') or hasattr(attr, '__set__') or
      hasattr(attr, '__delete__')
    ):
      mtype = type(attr)
      if mtype != staticmethod and mtype != classmethod:
        true_properties.add(m)
  return tuple(true_properties.difference(set(exclude)))


def wrap_method_once(fun: Callable[..., Any]) -> Callable[..., Any]:
  """Manages Module state for a given user-defined method.

  Args:
    fun: User-defined Module method to manage state for.
  Returns:
    Wrapped method.
  """
  # Don't rewrap methods that have already had the state management wrapper
  # applied in the decorator stack.  This wrapper should always be applied
  # before transformation wrappers.
  if hasattr(fun, 'method_handler_wrapped'):
    return fun

  @functools.wraps(fun)
  def wrapped_module_method(*args, **kwargs):
    # We might have incorrectly wrappped a callable
    # that is not a method. Check whether the first arg is self,
    # otherwise call the wrapped function as is.
    if args and isinstance(args[0], Module):
      self, args = args[0], args[1:]
      return self._call_wrapped_method(fun, args, kwargs)
    else:
      return fun(*args, **kwargs)
  wrapped_module_method.method_handler_wrapped = True  # type: ignore[attr-defined]
  return wrapped_module_method

def wrap_descriptor_once(descriptor) -> "DescriptorWrapper":
  """Wraps a descriptor to give better error messages.

  Args:
    prop: User-defined Module attribute descriptor.
  Returns:
    Wrapped descriptor.
  """
  # Don't rewrap descriptors.
  if isinstance(descriptor, DescriptorWrapper):
    return descriptor

  return create_descriptor_wrapper(descriptor)


def _wrap_hash(hash_fn: Callable[..., Any]) -> Callable[..., Any]:
  """Wraps a hash function with some check for Flax Modules."""
  @functools.wraps(hash_fn)
  def wrapped(self):
    if self.scope is not None:
      raise TypeError('Can\'t call __hash__ on modules that hold variables.')
    try:
      hash_value = hash_fn(self)
    except TypeError as exc:
      raise TypeError('Failed to hash Flax Module.  '
                      'The module probably contains unhashable attributes.  '
                      f'Module={self}') from exc
    return hash_value
  return wrapped


def _get_unbound_fn(method_or_fn: Callable[..., Any]) -> Callable[..., Any]:
  """Returns an unbound function from a method that is possibly bound.

  This means that if the passed function belongs of an instance of a class, then
  the returned function does no longer depend on the instance, which is passed
  as the first argument to the function.

  Args:
    method_or_fn: A class method or function.
  Returns:
    An unbound version of input function.
  """
  if (inspect.ismethod(method_or_fn) and
      isinstance(method_or_fn.__self__, Module)):  # pytype: disable=attribute-error
    method_or_fn = method_or_fn.__func__  # pytype: disable=attribute-error

  # The method should be callable, and it should have at least one argument
  # representing the class that is passed in.
  if (not callable(method_or_fn) or
      len(inspect.signature(method_or_fn).parameters) < 1):
    raise errors.ApplyModuleInvalidMethodError(method_or_fn)

  return method_or_fn

def _has_modules(x):
  if isinstance(x, Module):
    return True
  elif isinstance(x, (int, float, bool, str, np.ndarray, jnp.ndarray)):
    return False
  else:
    return any(isinstance(v, Module) for v in jax.tree_util.tree_leaves(x))

class SetupState(enum.IntEnum):
  # setup() has not been called.
  NEW = 0
  # setup() has been called outside a transform boundary.
  TRANSFORMED = 1
  # setup() has been called.
  DONE = 2


@dataclasses.dataclass
class _ModuleInternalState:
  """Ephemeral Module Evaluation State.

  For clarity, we collect all of the temporary flags and ephemeral state used by
  Modules for autonaming and error messages here, alongside the rules used
  to pass this ephemeral state across transform boundaries.
  """
  in_compact_method: bool = False
  in_setup: bool = False
  setup_called: SetupState = SetupState.NEW
  is_initialized: bool = False
  autoname_cursor: Dict[str, int] = dataclasses.field(default_factory=dict)
  children: Dict[str, Union[str, 'Module']] = dataclasses.field(
      default_factory=dict)

  def reset(self) -> None:
    """Resets transient state.

    This function is called after each module method, so only attributes that
    are method-dependent are reset.
    """
    self.in_compact_method = False
    self.in_setup = False
    self.autoname_cursor = dict()

  def export(self) -> '_ModuleInternalState':
    """Exports transform-preserved state across transform boundary."""
    setup_state = SetupState.TRANSFORMED if self.setup_called else SetupState.NEW
    cloned = _ModuleInternalState(
        in_compact_method=self.in_compact_method,
        in_setup=self.in_setup,
        setup_called=setup_state,
        is_initialized=self.is_initialized,
        autoname_cursor=dict(self.autoname_cursor))
    return cloned

  def reimport(self, other: '_ModuleInternalState') -> None:
    """Re-imports transform-preserved state from across transform boundary."""
    self.in_compact_method = other.in_compact_method
    self.in_setup = other.in_setup
    self.is_initialized = other.is_initialized
    self.autoname_cursor = dict(other.autoname_cursor)

_uninitialized_module_internal_state = _ModuleInternalState()


_UNDEFINED_COPY_PICKLE_METHODS = (
    '__getstate__', '__setstate__', '__getnewargs_ex__',
    '__reduce__', '__reduce_ex__', '__copy__', '__deepcopy__')


_caches: 'weakref.WeakKeyDictionary[Scope, weakref.WeakValueDictionary[FlaxId, Module]]' = (
    weakref.WeakKeyDictionary())


tuple_reduce = lambda xs, x: xs + (x,)
tuple_init = lambda: ()


capture_call_intermediates = lambda _, method_name: method_name == '__call__'


class ParentDescriptor:
  """Wraps parent module references in weak refs.

  This prevents reference cycles from forming via parent links which can lead
  to accidental OOMs in eager mode due to slow garbage collection as well as
  spurious tracer leaks during jit compilation.

  Note: "descriptors" are the underlying python mechanism for implementing
  dynamic @property decorators.  We need to use a raw descriptor instead of the
  more common decorator in order to force that the appropriate getter/setter
  logic applies in subclasses even after various dataclass transforms.
  """
  def __get__(self, obj, objtype=None):
    # check if obj is None, happens during %autoreload
    if obj is None:
      return None
    parent = object.__getattribute__(obj, "_parent_ref")
    return parent() if isinstance(parent, weakref.ReferenceType) else parent

  def __set__(self, obj, value):
    maybe_weak = weakref.ref(value) if isinstance(value, Module) else value
    object.__setattr__(obj, "_parent_ref", maybe_weak)


class Descriptor(Protocol):
  __isabstractmethod__: bool
  def __get__(self, obj, objtype=None) -> Any: ...
  def __set__(self, obj, value) -> None: ...
  def __delete__(self, obj) -> None: ...
  def __set_name__(self, owner, name) -> None: ...

class DescriptorWrapper:
  pass

def create_descriptor_wrapper(descriptor: Descriptor):
  """Creates a descriptor wrapper that calls a get_fn on the descriptor."""

  class _DescriptorWrapper(DescriptorWrapper):
    """A descriptor that can wrap any descriptor"""

    if hasattr(descriptor, '__isabstractmethod__'):
      __isabstractmethod__ = descriptor.__isabstractmethod__

    def __init__(self, wrapped: Descriptor):
      self.wrapped = wrapped

    # conditionally define descriptor methods
    if hasattr(descriptor, '__get__'):
      def __get__(self, *args, **kwargs):
        # here we will catch internal AttributeError and re-raise it as a
        # more informative and correct error message.
        try:
          return self.wrapped.__get__(*args, **kwargs)
        except AttributeError as e:
          raise errors.DescriptorAttributeError() from e

    if hasattr(descriptor, '__set__'):
      def __set__(self, *args, **kwargs):
        return self.wrapped.__set__(*args, **kwargs)

    if hasattr(descriptor, '__delete__'):
      def __delete__(self, *args, **kwargs):
        return self.wrapped.__delete__(*args, **kwargs)

    if hasattr(descriptor, '__set_name__'):
      def __set_name__(self, *args, **kwargs):
        self.wrapped.__set_name__(*args, **kwargs)

    def __getattr__(self, name):
      return getattr(self.wrapped, name)

  return _DescriptorWrapper(descriptor)

# Base Module definition.
# -----------------------------------------------------------------------------


@dataclass_transform()
class Module:
  """Base class for all neural network modules. Layers and models should subclass this class.

  All Flax Modules are Python 3.7
  `dataclasses <https://docs.python.org/3/library/dataclasses.html>`_. Since
  dataclasses take over ``__init__``, you should instead override :meth:`setup`,
  which is automatically called to initialize the module.

  Modules can contain submodules, and in this way can be nested in a tree
  structure. Submodels can be assigned as regular attributes inside the
  :meth:`setup` method.

  You can define arbitrary "forward pass" methods on your Module subclass.
  While no methods are special-cased, ``__call__`` is a popular choice because
  it allows you to use module instances as if they are functions::

    from flax import linen as nn

    class Module(nn.Module):
      features: Tuple[int, ...] = (16, 4)

      def setup(self):
        self.dense1 = Dense(self.features[0])
        self.dense2 = Dense(self.features[1])

      def __call__(self, x):
        return self.dense2(nn.relu(self.dense1(x)))

  Optionally, for more concise module implementations where submodules
  definitions are co-located with their usage, you can use the
  :meth:`compact` wrapper.
  """

  if typing.TYPE_CHECKING:

    def __init__(self, *args, **kwargs):
      # this stub makes sure pytype accepts constructor arguments.
      pass

    def __call__(self, *args, **kwargs) -> Any:
      # this stub allows pytype to accept Modules as Callables.
      pass

  @classmethod
  def __init_subclass__(cls, **kwargs: Any) -> None:
    """Automatically initializes all subclasses as custom dataclasses."""
    super().__init_subclass__(**kwargs)
    # All Flax Modules are dataclasses.  We force this convention since
    # it encourages the stateless behavior needed to clone module instances for
    # functional transformation.  Instead of using a python metaclass, we
    # automatically transform Modules into dataclasses at subclass creation
    # time, and we set the last dataclass arguments to `parent` and `name`.
    cls._customized_dataclass_transform()
    # We wrap user-defined methods including setup and __call__ to enforce
    # a number of different checks and to provide clear error messages.
    cls._verify_single_or_no_compact()
    cls._wrap_module_attributes()
    # Set empty class defaults.
    cls._state = _uninitialized_module_internal_state # type: ignore[attr-defined]
    cls.scope: Optional[Scope] = None # type: ignore
    # Handles weak referencing of parent Modules to prevent reference cycles.
    cls._parent_ref = None # type: ignore[attr-defined]
    cls.parent = ParentDescriptor() # type: ignore[attr-defined]

  @classmethod
  def _customized_dataclass_transform(cls):
    """Transforms `cls` into a dataclass, with custom additional behavior.

    1. Inject `parent` and `name` fields.  (If they are already present,
       then check that they have the expected types.)
    2. Set compare, hash, and repr to False for non-init fields.
    3. Generate a hash function (if not provided by cls).
    """
    # Check reserved attributes have expected type annotations.
    annotations = dict(cls.__dict__.get('__annotations__', {}))
    if annotations.get('parent', _ParentType) != _ParentType:
      raise errors.ReservedModuleAttributeError(annotations)
    if annotations.get('name', str) not in ('str', str, Optional[str]):
      raise errors.ReservedModuleAttributeError(annotations)

    # any non-init field will only be set in setup
    # During __hash__ and __eq__ the field is not set yet
    # so it should not be used in compare, hash or repr.
    for field in annotations:
      field_meta = getattr(cls, field, None)
      if isinstance(field_meta, dataclasses.Field) and not field_meta.init:
        field_meta.compare = False
        field_meta.hash = False
        field_meta.repr = False

    extra_fields = [('parent', _ParentType,
                     kw_only_dataclasses.field(
                         repr=False, default=_unspecified_parent,
                         kw_only=True)),
                    ('name', Optional[str],
                     kw_only_dataclasses.field(default=None, kw_only=True))]

    # Now apply dataclass transform (which operates in-place).
    # Do generate a hash function only if not provided by the class.
    kw_only_dataclasses.dataclass(
        cls,
        unsafe_hash='__hash__' not in cls.__dict__,
        repr=False,
        extra_fields=extra_fields)  # pytype: disable=wrong-keyword-args
    cls.__hash__ = _wrap_hash(cls.__hash__)

  @classmethod
  def _verify_single_or_no_compact(cls):
    """Statically verifies that at most a single method is labelled compact."""
    methods = [m[0] for m in inspect.getmembers(cls, predicate=callable)]
    n_compact_fns = len([method_name for method_name in methods
                         if hasattr(getattr(cls, method_name), 'compact')])
    if n_compact_fns > 1:
      raise errors.MultipleMethodsCompactError()

  @classmethod
  def _wrap_module_attributes(cls):
    """Wraps user-defined non-inherited methods and descriptors with state
    management functions.
    """
    # wrap methods
    method_exclusions = ([f.name for f in dataclasses.fields(cls)] +
                  ['__eq__', '__repr__', '__init__', '__hash__',
                   '__post_init__'])
    for key in _get_local_method_names(cls, exclude=method_exclusions):
      method = getattr(cls, key)
      if hasattr(method, 'nowrap'):
        continue
      setattr(cls, key, wrap_method_once(method))

    # wrap descriptors
    descriptor_exclusions = ([f.name for f in dataclasses.fields(cls)] +
                             ['parent', '__dict__'])
    for key in _get_local_descriptor_names(cls, descriptor_exclusions):
      # don't use getattr here, since it will call the descriptor
      descriptor = cls.__dict__[key]
      if hasattr(descriptor, 'nowrap'):
        continue
      setattr(cls, key, wrap_descriptor_once(descriptor))
    return cls

  def _call_wrapped_method(self, fun, args, kwargs):
    """"Calls a wrapped method.

    This function is responsible for setting up the thread local state
    correctly before calling the method and cleaning up afterwards.
    This includes storing intermediates, setup of the compact scope,
    and making sure setup is called before any other method.

    Args:
      fun: The wrapped method.
      args: Named arguments passed to ``fun``.
      kwargs: Keyword arguments passed to ``fun``.

    Returns:
      The results of calling ``fun``.
    """
    is_compact_method = hasattr(fun, 'compact')
    fun_name = getattr(fun, '__name__', 'unnamed_function')
    is_setup_method = fun_name == 'setup'
    add_call_info = not is_setup_method and len(_context.call_info_stack) > 0
    # We lazily call setup() only when needed.
    if is_setup_method:
      if self.scope is None:
        raise errors.CallSetupUnboundModuleError()
      is_recurrent = self._state.in_setup
      self._state.in_setup = True
    else:
      self._try_setup()

    if is_compact_method:
      if self.scope is None:
        raise errors.CallCompactUnboundModuleError()
      is_recurrent = self._state.in_compact_method
      self._state.in_compact_method = True
    _context.module_stack.append(self)
    try:
      # get call info
      if add_call_info:
        call_index = _context.call_info_stack[-1].get_call_index(self)
        scope_path = jax.tree_util.tree_map(_fix_path_part, self.scope.path)

      # call method
      if _use_named_call:
        with jax.named_scope(_derive_profiling_name(self, fun)):
          y = fun(self, *args, **kwargs)
      else:
        y = fun(self, *args, **kwargs)

      if _context.capture_stack:
        filter_fn = _context.capture_stack[-1]
        if filter_fn and filter_fn(self, fun_name):
          self.sow('intermediates', fun_name, y)
      if add_call_info:
        _args, _kwargs, _y = flax.linen.summary._represent_tree((args, kwargs, y))
        _context.call_info_stack[-1].calls.append(
          _CallInfo(call_index, scope_path, type(self), fun.__name__, _args, _kwargs, _y))
      return y
    finally:
      _context.module_stack.pop()
      if is_compact_method:
        object.__setattr__(self, 'scope', self.scope.rewound())
      # setup or compact calls can be recurrent for example due to super calls
      # resetting the state would cause is compact/setup method
      # to be set to False prematurely.
      if (is_compact_method or is_setup_method) and not is_recurrent:
        self._state.reset()

  def __setattr__(self, name: str, val: Any):
    """Sets an attribute on this Module.

    We overload setattr solely to support pythonic naming via assignment of
    submodules in the special :meth:`setup` function::

      self.submodule_name = MyModule(...)

    We also support lists and other general pytrees, e.g.::

      self.submodules = [MyModule0(..), MyModule1(..), ...]

    Args:
      name: Attribute to set.
      val: Value of the attribute.
    """
    fields = self.__dataclass_fields__  # pytype: disable=attribute-error
    is_dataclass_attr = name in fields and fields[name].init

    if not self._state.in_setup:
      if not self._state.is_initialized:
        # Setting attributes before end of Module.__post_init__()
        object.__setattr__(self, name, val)
        return
      else:
        # We're past all initialization and setup logic:
        # Raises a TypeError just like frozen python dataclasses.
        raise errors.SetAttributeFrozenModuleError(
            self.__class__.__name__, name, val)

    # We're inside the setup() method:
    if is_dataclass_attr:
      # These names are specified as dataclass fields. They should not be
      # initialized within the setup() method, but can be modified freely
      # before it.
      raise errors.SetAttributeInModuleSetupError()

    # Values (that may be variables or submodules) are being defined and
    # attached in setup(), we run some extra logic in that case.
    self._register_submodules(name, val)

  def __getattr__(self, name: str) -> Any:
    """Call setup() before getting any setup-defined attributes."""
    # We don't want to return anything for python copy / pickle methods.
    if name in _UNDEFINED_COPY_PICKLE_METHODS:
      raise AttributeError()
    self._try_setup()
    if name in self.__dict__:
      return self.__dict__[name]
    else:
      msg = f'"{self.__class__.__name__}" object has no attribute "{name}".'
      if self.scope is None:
        msg += (f' If "{name}" is defined in \'.setup()\', remember these fields '
          'are only accessible from inside \'init\' or \'apply\'.')
      raise AttributeError(msg)

  def __getattribute__(self, name):
    """Call setup() before accessing any submodule attributes."""
    # NB: all code here is very "hot" and will be run very frequently.
    if ('_submodule_dataclass_fields' in object.__getattribute__(self, '__dict__')
        and name in object.__getattribute__(self, '_submodule_dataclass_fields')):
      object.__getattribute__(self, '_try_setup')()
    # always run original python __getattribute__
    return object.__getattribute__(self, name)

  def __dir__(self) -> List[str]:
    """Call setup() before listing attributes."""
    self._try_setup()
    return object.__dir__(self)  # type: ignore

  def __post_init__(self) -> None:
    # DO NOT REMOVE - Marker for internal logging.
    # In dataclasses, __init__ is overridden to process dataclass arguments,
    # and __post_init__ is called immediately afterwards. Here, depending on the
    # type of `parent` passed to initialize the Module, we either defer
    # initialization, attach this Module as a submodule of a parent, or bind
    # this Module at the top-level to variables and rngs.

    object.__setattr__(self, '_id', uuid())
    object.__setattr__(self, '_state', _ModuleInternalState())

    # Typically we set the parent based on the dynamic module context.
    if self.parent is _unspecified_parent:  # pytype: disable=attribute-error
      object.__setattr__(self, 'parent', _context.module_stack[-1])

    # find all dataclass fields that have submodules
    submodule_dataclass_fields = {
      field.name for field in dataclasses.fields(self)
      if field.name not in ('parent', 'name')
      if field.name in self.__dict__ # ignore fields that have not been set
      if _has_modules(getattr(self, field.name))
    }
    object.__setattr__(self, '_submodule_dataclass_fields', submodule_dataclass_fields)

    # Initialization is deferred for top level Modules or any other "orphan"
    # Modules until attachment by __setattr__ i.e. MyModule(..., parent=None)
    if self.parent is None:
      return

    # Register submodule on parent Module.
    if isinstance(self.parent, Module):
      # When initializing an unnamed Module inside setup()
      # initialization is deferred until attachment by __setattr__
      # i.e. self.mymodule = MyModule(...)
      self.name: Optional[str]
      if self.parent._state.in_setup and self.name is None:  # pytype: disable=attribute-error
        return
      if not self.parent._initialization_allowed:
        raise errors.AssignSubModuleError(self.__class__.__name__)
      # Autonaming of submodules.
      if self.name is None:  # pytype: disable=attribute-error
        prefix = f'{self.__class__.__name__}'
        cursor = self.parent._state.autoname_cursor.get(prefix, 0)
        self.name = f'{prefix}_{cursor}'
        self.parent._state.autoname_cursor[prefix] = cursor + 1
      # Allow scope aliasing under transforms for submodules defined in setup.
      reuse_scopes = (self.parent._state.in_setup and
                      self.parent._state.setup_called == SetupState.TRANSFORMED)
      # Perform name-collision check.
      if self.parent._name_taken(self.name, self, reuse_scopes=reuse_scopes):
        parent_class = self.parent.__class__.__name__
        raise errors.NameInUseError('submodule', self.name, parent_class)
      # Finalize attachment to parent and scope initialization.
      self.parent._state.children[self.name] = self
      object.__setattr__(
          self, 'scope', self.parent.scope.push(self.name, reuse=reuse_scopes))

    # Top-level invocation with a functional Scope.
    elif isinstance(self.parent, Scope):
      object.__setattr__(self, 'scope', self.parent)
    else:
      raise ValueError('parent must be None, Module or Scope')

    self._state.is_initialized = True

  def __repr__(self) -> str:
    return _module_repr(self)

  def setup(self) -> None:
    """Initializes a Module lazily (similar to a lazy ``__init__``).

    ``setup`` is called once lazily on a module instance when a module
    is bound, immediately before any other methods like ``__call__`` are
    invoked, or before a ``setup``-defined attribute on `self` is accessed.

    This can happen in three cases:

      1. Immediately when invoking :meth:`apply`, :meth:`init` or
         :meth:`init_and_output`.

      2. Once the module is given a name by being assigned to an attribute of
         another module inside the other module's ``setup`` method
         (see :meth:`__setattr__`)::

           class MyModule(nn.Module):
             def setup(self):
               submodule = Conv(...)

               # Accessing `submodule` attributes does not yet work here.

               # The following line invokes `self.__setattr__`, which gives
               # `submodule` the name "conv1".
               self.conv1 = submodule

               # Accessing `submodule` attributes or methods is now safe and
               # either causes setup() to be called once.

      3. Once a module is constructed inside a method wrapped with
         :meth:`compact`, immediately before another method is called or
         ``setup`` defined attribute is accessed.
    """
    pass

  def _register_submodules(self, name, val):
    """Registers a submodule."""
    assert self.scope, 'Trying to register submodules on unbound scope.'
    root = self.scope.root
    cache = _caches.get(root, weakref.WeakValueDictionary())
    _caches[root] = cache
    queue = []
    def adopt_attr_modules(cache, queue, suffix, subvalue):
      if isinstance(subvalue, Module):
        adopted_name = None
        if subvalue.parent is None:
          # Module was passed from outside. It needs to be cloned.
          # Outside modules are named by attachment, not an outer name,
          # UNLESS we're using new relaxed naming, in which case an existing
          # name will be used.
          if config.flax_relaxed_naming:
            adopted_name = object.__getattribute__(subvalue, 'name')
          object.__setattr__(subvalue, 'name', None)
          # Preserve sharing-by-reference relationships during adoption
          # via cache keyed on unique instance ids.
          key = subvalue._id
          if key in cache:
            subvalue = cache[key]
          else:
            # We must bind to local variable before adding to weakvalue dict.
            subvalue = subvalue.clone()
            cache[key] = subvalue
        if subvalue.name is None:
          object.__setattr__(subvalue, 'parent', self)
          if adopted_name is None:
            adopted_name = f'{name}{suffix}'
          object.__setattr__(subvalue, 'name', adopted_name)
          queue.append(subvalue)
      return subvalue
    val = _freeze_attr(_map_over_modules_in_tree(
        functools.partial(adopt_attr_modules, cache, queue), val))
    object.__setattr__(self, name, val)
    for x in queue:
      x.__post_init__()

  def _try_setup(self, shallow: bool = False) -> None:
    """Tries to setup module if scope is available and setup has not been called yet."""
    if (self.scope
        and not self._state.in_setup
        and self._state.setup_called != SetupState.DONE):
      try:
        self._state.in_setup = True
        # A shallow setup will only register attribute submodules but it does
        # not call the user's setup. This avoids running before a
        # transformation.
        for field in dataclasses.fields(self):
          if field.name != 'parent' and field.init:
            self._register_submodules(field.name, getattr(self, field.name))
        if not shallow:
          self.setup()
        # We run static checks abstractly once for setup before any transforms
        # to detect name collisions and other python errors.
        elif self._state.setup_called == SetupState.NEW:
          self._validate_setup()
      finally:
        self._state.in_setup = False
        self._state.setup_called = SetupState.DONE

  def _validate_setup(self) -> None:
    """Abstractly evaluates setup only to run static checks."""
    def run_setup_only(x):
      wrapped_id = wrap_method_once(lambda m, x: x)
      with TestScope({}, rngs={}, mutable=True).temporary() as root:
        return wrapped_id(self.clone(parent=root), x)
    _ = jax.eval_shape(run_setup_only, 0)

  def _name_taken(self,
                  name: str,
                  module: Optional['Module'] = None,
                  reuse_scopes: bool = False,
                  collection: Optional[str] = None) -> bool:
    # with relaxed naming don't force non-overlap with python attribute names.
    if config.flax_relaxed_naming:
      if reuse_scopes:
        return False
      return self.scope.name_reserved(name, collection)
    if name in _all_names_on_object(self):
      val = getattr(self, name, None)
      if module is not None and val is module:
        # name is taken by the value itself because
        # field assignment happened before naming
        return False
      return True
    # Check for the existence of name in the scope object.
    if reuse_scopes:
      return False
    return name in self.scope.reservations

  @property
  def _initialization_allowed(self):
    return self._state.in_setup or self._state.in_compact_method

  def clone(self: M, *,
            parent: Optional[Union[Scope, 'Module']] = None,
            **updates) -> M:
    """Creates a clone of this Module, with optionally updated arguments.

    Args:
      parent: The parent of the clone. The clone will have no parent if no
        explicit parent is specified.
      **updates: Attribute updates.
    Returns:
      A clone of the this Module with the updated attributes and parent.
    """
    attrs = {f.name: getattr(self, f.name) for f in dataclasses.fields(self) if f.init}
    attrs.update(parent=parent, **updates)
    return self.__class__(**attrs)

  def variable(self, col: str, name: str,
               init_fn: Optional[Callable[..., Any]] = None,
               *init_args,
               unbox: bool = True) -> Variable:
    """Declares and returns a variable in this Module.

    See :mod:`flax.core.variables` for more information. See also :meth:`param`
    for a shorthand way to define read-only variables in the "params"
    collection.

    Contrary to :meth:`param`, all arguments passing using `init_fn` should be
    passed on explicitly::

      key = self.make_rng('stats')
      mean = self.variable('stats', 'mean', lecun_normal(), key, (2, 2))

    In the example above, the function `lecun_normal` expects two arguments:
    `key` and `shape`, and both have to be passed on. The PRNG for `stats` has
    to be provided explicitly when calling :meth:`init` and :meth:`apply`.

    Args:
      col: The variable collection name.
      name: The variable name.
      init_fn: The function that will be called to compute the initial value
        of this variable. This function will only be called the first time
        this variable is used in this module. If None, the variable must
        already be initialized otherwise an error is raised.
      *init_args: The arguments to pass to init_fn.
      unbox: If True, ``AxisMetadata`` instances are replaced by their unboxed
        value, see ``flax.nn.meta.unbox`` (default: True).

    Returns:
      A :class:`flax.core.variables.Variable` that can be read or set via
      ".value" attribute. Throws an error if the variable exists already.
    """
    if not self._initialization_allowed:
      raise ValueError(
          'Variables must be initialized in `setup()` or in a method '
          'wrapped in `@compact`')
    if self._name_taken(name, collection=col):
      raise errors.NameInUseError('variable', name, self.__class__.__name__)
    v = self.scope.variable(col, name, init_fn, *init_args, unbox=unbox)
    self._state.children[name] = col
    return v

  def param(self, name: str, init_fn: Callable[..., T], *init_args,
            unbox: bool = True) -> T:
    """Declares and returns a parameter in this Module.

    Parameters are read-only variables in the collection named "params". See
    :mod:`flax.core.variables` for more details on variables.

    The first argument of `init_fn` is assumed to be a PRNG key, which is
    provided automatically and does not have to be passed using `init_args`::

      mean = self.param('mean', lecun_normal(), (2, 2))

    In the example above, the function `lecun_normal` expects two arguments:
    `key` and `shape`, but only `shape` has to be provided explicitly; `key`
    is set automatically using the PRNG for `params` that is passed when
    initializing the module using :meth:`init`.

    Args:
      name: The parameter name.
      init_fn: The function that will be called to compute the initial value
        of this variable. This function will only be called the first time
        this parameter is used in this module.
      *init_args: The arguments to pass to init_fn.
      unbox: If True, ``AxisMetadata`` instances are replaced by their unboxed
        value, see ``flax.nn.meta.unbox`` (default: True).

    Returns:
      The value of the initialized parameter. Throws an error if the parameter
      exists already.
    """
    if not self._initialization_allowed:
      raise ValueError(
          'Parameters must be initialized in `setup()` or in a method '
          'wrapped in `@compact`')
    if self._name_taken(name, collection='params'):
      raise errors.NameInUseError('param', name, self.__class__.__name__)
    v = self.scope.param(name, init_fn, *init_args, unbox=unbox)
    self._state.children[name] = 'params'
    return v

  def has_variable(self, col: str, name: str) -> bool:
    """Checks if a variable of given collection and name exists in this Module.

    See :mod:`flax.core.variables` for more explanation on variables and
    collections.

    Args:
      col: The variable collection name.
      name: The name of the variable.
    Returns:
      True if the variable exists.
    """
    if self.scope is None:
      raise ValueError("Can't access variables on unbound modules")
    return self.scope.has_variable(col, name)

  def is_mutable_collection(self, col: str) -> bool:
    """Returns true if the collection `col` is mutable."""
    if self.scope is None:
      raise ValueError("Can't check mutability on unbound modules")
    return self.scope.is_mutable_collection(col)

  def has_rng(self, name: str) -> bool:
    """Returns true if a PRNGSequence with name `name` exists."""
    if self.scope is None:
      raise ValueError("Can't query for RNGs on unbound modules")
    return self.scope.has_rng(name)

  def make_rng(self, name: str) -> PRNGKey:
    """Returns a new RNG key from a given RNG sequence for this Module.

    The new RNG key is split from the previous one. Thus, every call to
    `make_rng` returns a new RNG key, while still guaranteeing full
    reproducibility.

    TODO: Link to Flax RNG design note.

    Args:
      name: The RNG sequence name.
    Returns:
      The newly generated RNG key.
    """
    if self.scope is None:
      raise ValueError("Can't use RNGs on unbound modules")
    return self.scope.make_rng(name)

  def is_initializing(self) -> bool:
    """Returns True if running under self.init(...) or nn.init(...)().

    This is a helper method to handle the common case of simple initialization
    where we wish to have setup logic occur when only called under
    ``module.init`` or ``nn.init``.  For more complicated multi-phase
    initialization scenarios it is better to test for the mutability of
    particular variable collections or for the presence of particular
    variables that potentially need to be initialized.
    """
    if self.scope is None:
      raise ValueError("Can't check if running under init() on unbound modules")
    return self.scope.get_flag('initializing', False)

  def _module_checks(self):
    """Run standard runtime checks."""

    if not isinstance(self, Module):
      raise errors.InvalidInstanceModuleError()

    overridden_post_init = self.__post_init__ != Module.__post_init__
    if overridden_post_init and not hasattr(self, "_id"):
      raise errors.IncorrectPostInitOverrideError()

  @traceback_util.api_boundary
  def bind(self: M,
           variables: VariableDict,
           *args,
           rngs: Optional[RNGSequences] = None,
           mutable: CollectionFilter = False) -> M:
    """Creates an interactive Module instance by binding variables and RNGs.

    ``bind`` provides an "interactive" instance of a Module directly without
    transforming a function with ``apply``. This is particularly useful for
    debugging zand interactive use cases like notebooks where a function would
    limit the ability to split up code into different cells.

    Once the variables (and optionally RNGs) are bound to a ``Module`` it
    becomes a stateful object. Note that idiomatic JAX is functional and
    therefore an interactive instance does not mix well with vanilla JAX APIs.
    ``bind()`` should only be used for interactive experimentation, and in all
    other cases we strongly encourage users to use ``apply()`` instead.

    Example::

      import jax
      import jax.numpy as jnp
      import flax.linen as nn

      class AutoEncoder(nn.Module):
        def setup(self):
          self.encoder = nn.Dense(3)
          self.decoder = nn.Dense(5)

        def __call__(self, x):
          return self.decoder(self.encoder(x))

      x = jnp.ones((16, 9))
      ae = AutoEncoder()
      variables = ae.init(jax.random.PRNGKey(0), x)
      model = ae.bind(variables)
      z = model.encoder(x)
      x_reconstructed = model.decoder(z)

    Args:
      variables: A dictionary containing variables keyed by variable
        collections. See :mod:`flax.core.variables` for more details
        about variables.
      *args: Named arguments (not used).
      rngs: a dict of PRNGKeys to initialize the PRNG sequences.
      mutable: Can be bool, str, or list. Specifies which collections should be
        treated as mutable:
          ``bool``: all/no collections are mutable.
          ``str``: The name of a single mutable collection.
          ``list``: A list of names of mutable collections.

    Returns:
      A copy of this instance with bound variables and RNGs.
    """
    Module._module_checks(self)

    del args
    scope = core.bind(variables, rngs=rngs, mutable=mutable)
    return self.clone(parent=scope)

  def unbind(self: M) -> Tuple[M, VariableDict]:
    """Returns an unbound copy of a Module and its variables.

    ``unbind`` helps create a stateless version of a bound Module.

    An example of a common use case: to extract a sub-Module defined inside
    ``setup()`` and its corresponding variables: 1) temporarily ``bind`` the parent
    Module; and then 2) ``unbind`` the desired sub-Module. (Recall that ``setup()``
    is only called when the Module is bound.)::

      class AutoEncoder(nn.Module):
        def setup(self):
          self.encoder = Encoder()
          self.decoder = Decoder()

        def __call__(self, x):
          return self.decoder(self.encoder(x))

      module = AutoEncoder()
      variables = module.init(jax.random.PRNGKey(0), jnp.ones((1, 784)))
      ...
      # Extract the Encoder sub-Module and its variables
      encoder, encoder_vars = module.bind(variables).encoder.unbind()

    Returns:
      A tuple with an unbound copy of this Module and its variables.
    """
    Module._module_checks(self)

    if self.scope is None:
      raise errors.CallUnbindOnUnboundModuleError()

    variables = self.variables
    module = self.clone()
    return module, variables

  @traceback_util.api_boundary
  def apply(self,
            variables: VariableDict,
            *args,
            rngs: Optional[RNGSequences] = None,
            method: Union[Callable[..., Any], str, None] = None,
            mutable: CollectionFilter = False,
            capture_intermediates: Union[bool, Callable[['Module', str], bool]] = False,
            **kwargs) -> Union[Any, Tuple[Any, FrozenVariableDict]]:
    """Applies a module method to variables and returns output and modified variables.

    Note that `method` should be set if one would like to call `apply` on a
    different class method than ``__call__``. For instance, suppose a
    Transformer modules has a method called `encode`, then the following calls
    `apply` on that method::

      model = Transformer()
      encoded = model.apply({'params': params}, x, method=Transformer.encode)

    If a function instance is provided, the unbound function is used. For
    instance, the example below is equivalent to the one above::

      encoded = model.apply({'params': params}, x, method=model.encode)

    You can also pass a string to a callable attribute of the module. For
    example, the previous can be written as::

      encoded = model.apply({'params': params}, x, method='encode')

    Note ``method`` can also be a function that is not defined in
    ``Transformer``. In that case, the function should have at least one
    argument representing an instance of the Module class::

      def other_fn(instance, ...):
        instance.some_module_attr(...)
        ...

      model.apply({'params': params}, x, method=other_fn)

    Args:
      variables: A dictionary containing variables keyed by variable
        collections. See :mod:`flax.core.variables` for more details
        about variables.
      *args: Named arguments passed to the specified apply method.
      rngs: a dict of PRNGKeys to initialize the PRNG sequences.
        The "params" PRNG sequence is used to initialize parameters.
      method: A function to call apply on. This is generally a function in the
        module. If provided, applies this method. If not provided, applies the
        ``__call__`` method of the module. A string can also be provided to
        specify a method by name.
      mutable: Can be bool, str, or list. Specifies which collections should be
               treated as mutable: ``bool``: all/no collections are mutable.
               ``str``: The name of a single mutable collection. ``list``: A
               list of names of mutable collections.
      capture_intermediates: If `True`, captures intermediate return values
        of all Modules inside the "intermediates" collection. By default only
        the return values of all ``__call__`` methods are stored. A function can
        be passed to change the filter behavior. The filter function takes
        the Module instance and method name and returns a bool indicating
        whether the output of that method invocation should be stored.
      **kwargs: Keyword arguments passed to the specified apply method.
    Returns:
      If ``mutable`` is False, returns output. If any collections are
      mutable, returns ``(output, vars)``, where ``vars`` are is a dict
      of the modified collections.
    """
    Module._module_checks(self)

    if isinstance(method, str):
      attribute_name = method
      method = getattr(self, attribute_name)
      if not callable(method):
        class_name = type(self).__name__
        raise TypeError(f'\'{class_name}.{attribute_name}\' must be a callable, got {type(method)}.')
    elif method is None:
      method = self.__call__
    method = _get_unbound_fn(method)
    return apply(
        method, self,
        mutable=mutable,
        capture_intermediates=capture_intermediates,
    )(variables, *args, **kwargs, rngs=rngs)

  @traceback_util.api_boundary
  def init_with_output(self,
                       rngs: Union[PRNGKey, RNGSequences],
                       *args,
                       method: Union[Callable[..., Any], str, None] = None,
                       mutable: CollectionFilter = DenyList('intermediates'),
                       capture_intermediates: Union[bool, Callable[['Module', str], bool]] = False,
                       **kwargs) -> Tuple[Any, FrozenVariableDict]:
    """Initializes a module method with variables and returns output and modified variables.

    Args:
      rngs: The rngs for the variable collections.
      *args: Named arguments passed to the init function.
      method: An optional method. If provided, applies this method. If not
        provided, applies the ``__call__`` method. A string can also be'
        provided to specify a method by name.
      mutable: Can be bool, str, or list. Specifies which collections should be
        treated as mutable: ``bool``: all/no collections are mutable.
        ``str``: The name of a single mutable collection. ``list``: A
        list of names of mutable collections. By default all collections
        except "intermediates" are mutable.
      capture_intermediates: If `True`, captures intermediate return values
        of all Modules inside the "intermediates" collection. By default only
        the return values of all ``__call__`` methods are stored. A function can
        be passed to change the filter behavior. The filter function takes
        the Module instance and method name and returns a bool indicating
        whether the output of that method invocation should be stored.
      **kwargs: Keyword arguments passed to the init function.
    Returns:
      `(output, vars)``, where ``vars`` are is a dict of the modified
      collections.
    """
    Module._module_checks(self)

    if not isinstance(rngs, dict):
      if not core.scope._is_valid_rng(rngs):
        raise errors.InvalidRngError(
            'RNGs should be of shape (2,) or KeyArray in Module '
            f'{self.__class__.__name__}, but rngs are: {rngs}')
      rngs = {'params': rngs}

    if isinstance(method, str):
      attribute_name = method
      method = getattr(self, attribute_name)
      if not callable(method):
        class_name = type(self).__name__
        raise TypeError(f'\'{class_name}.{attribute_name}\' must be a callable, got {type(method)}.')
    elif method is None:
      method = self.__call__
    method = _get_unbound_fn(method)
    return init_with_output(
        method,
        self,
        mutable=mutable,
        capture_intermediates=capture_intermediates
    )(rngs, *args, **kwargs)

  @traceback_util.api_boundary
  def init(self,
           rngs: Union[PRNGKey, RNGSequences],
           *args,
           method: Union[Callable[..., Any], str, None] = None,
           mutable: CollectionFilter = DenyList('intermediates'),
           capture_intermediates: Union[bool, Callable[['Module', str], bool]] = False,
           **kwargs) -> FrozenVariableDict:
    """Initializes a module method with variables and returns modified variables.

    ``init`` takes as first argument either a single ``PRNGKey``, or a dictionary mapping variable collections names to their ``PRNGKeys``, and will call ``method`` (which is the module's ``__call__`` function by default) passing ``*args`` and ``**kwargs``, and returns
    a dictionary of initialized variables.

    Example::

      >>> import flax.linen as nn
      >>> import jax.numpy as jnp
      >>> import jax
      ...
      >>> class Foo(nn.Module):
      ...   @nn.compact
      ...   def __call__(self, x, train):
      ...     x = nn.Dense(16)(x)
      ...     x = nn.BatchNorm(use_running_average=not train)(x)
      ...     x = nn.relu(x)
      ...     return nn.Dense(1)(x)
      ...
      >>> module = Foo()
      >>> key = jax.random.PRNGKey(0)
      >>> variables = module.init(key, jnp.empty((1, 7)), train=False)

    If you pass a single ``PRNGKey``, Flax will use it to feed the ``'params'`` RNG stream.
    If you want to use a different RNG stream or need to use multiple streams, you must pass a
    dictionary mapping each RNG stream name to its corresponding ``PRNGKey`` to ``init``.

    Example::

      >>> class Foo(nn.Module):
      ...   @nn.compact
      ...   def __call__(self, x, train):
      ...     x = nn.Dense(16)(x)
      ...     x = nn.BatchNorm(use_running_average=not train)(x)
      ...     x = nn.relu(x)
      ...
      ...     # Add gaussian noise
      ...     noise_key = self.make_rng('noise')
      ...     x = x + jax.random.normal(noise_key, x.shape)
      ...
      ...     return nn.Dense(1)(x)
      ...
      >>> module = Foo()
      >>> rngs = {'params': jax.random.PRNGKey(0), 'noise': jax.random.PRNGKey(1)}
      >>> variables = module.init(rngs, jnp.empty((1, 7)), train=False)

    Jitting `init` initializes a model lazily using only the shapes of the
    provided arguments, and avoids computing the forward pass with actual
    values. Example::

      >>> module = nn.Dense(1)
      >>> init_jit = jax.jit(module.init)
      >>> variables = init_jit(jax.random.PRNGKey(0), jnp.empty((1, 7)))

    ``init`` is a light wrapper over ``apply``, so other ``apply`` arguments like
    ``method``, ``mutable``, and ``capture_intermediates`` are also available.

    Args:
      rngs: The rngs for the variable collections.
      *args: Named arguments passed to the init function.
      method: An optional method. If provided, applies this method. If not
        provided, applies the ``__call__`` method. A string can also be
        provided to specify a method by name.
      mutable: Can be bool, str, or list. Specifies which collections should be
        treated as mutable: ``bool``: all/no collections are mutable.
        ``str``: The name of a single mutable collection. ``list``: A
        list of names of mutable collections. By default all collections
        except "intermediates" are mutable.
      capture_intermediates: If `True`, captures intermediate return values
        of all Modules inside the "intermediates" collection. By default only
        the return values of all ``__call__`` methods are stored. A function can
        be passed to change the filter behavior. The filter function takes
        the Module instance and method name and returns a bool indicating
        whether the output of that method invocation should be stored.
      **kwargs: Keyword arguments passed to the init function.
    Returns:
      The initialized variable dict.
    """
    Module._module_checks(self)

    _, v_out = self.init_with_output(
        rngs,
        *args,
        method=method,
        mutable=mutable,
        capture_intermediates=capture_intermediates,
        **kwargs)
    return v_out

  @property
  def variables(self) -> VariableDict:
    """Returns the variables in this module."""
    if self.scope is None:
      raise ValueError("Can't access variables on unbound modules")
    return self.scope.variables()

  def get_variable(self, col: str, name: str, default: Optional[T] = None) -> T:
    """Retrieves the value of a Variable.

    Args:
      col: the variable collection.
      name: the name of the variable.
      default: the default value to return if the variable does not exist in
        this scope.

    Returns:
      The value of the input variable, of the default value if the variable
      doesn't exist in this scope.
    """
    if self.scope is None:
      raise ValueError("Can't access variables on unbound modules")
    return self.scope.get_variable(col, name, default)

  def put_variable(self, col: str, name: str, value: Any):
    """Updates the value of the given variable if it is mutable, or an error otherwise.

    Args:
      col: the variable collection.
      name: the name of the variable.
      value: the new value of the variable.
    """
    if self.scope is None:
      raise ValueError("Can't access variables on unbound modules")
    self.scope.put_variable(col, name, value)

  @overload
  def sow(self, col: str, name: str, value: Any) -> bool:
    ...

  @overload
  def sow(self, col: str, name: str, value: T,
          reduce_fn: Callable[[K, T], K] = tuple_reduce,
          init_fn: Callable[[], K] = tuple_init) -> bool: # type: ignore
    ...

  def sow(self, col: str, name: str, value: T,
          reduce_fn: Callable[[K, T], K] = tuple_reduce,
          init_fn: Callable[[], K] = tuple_init) -> bool: # type: ignore
    """Stores a value in a collection.

    Collections can be used to collect intermediate values without
    the overhead of explicitly passing a container through each Module call.

    If the target collection is not mutable `sow` behaves like a no-op
    and returns `False`.

    Example::

      import jax
      import jax.numpy as jnp
      import flax.linen as nn

      class Foo(nn.Module):
        @nn.compact
        def __call__(self, x):
          h = nn.Dense(4)(x)
          self.sow('intermediates', 'h', h)
          return nn.Dense(2)(h)

      x = jnp.ones((16, 9))
      model = Foo()
      variables = model.init(jax.random.PRNGKey(0), x)
      y, state = model.apply(variables, x, mutable=['intermediates'])
      print(state['intermediates'])  # {'h': (...,)}

    By default the values are stored in a tuple and each stored value
    is appended at the end. This way all intermediates can be tracked when
    the same module is called multiple times. Alternatively, a custom
    init/reduce function can be passed::

      class Foo2(nn.Module):
        @nn.compact
        def __call__(self, x):
          init_fn = lambda: 0
          reduce_fn = lambda a, b: a + b
          self.sow('intermediates', 'h', x,
                   init_fn=init_fn, reduce_fn=reduce_fn)
          self.sow('intermediates', 'h', x * 2,
                   init_fn=init_fn, reduce_fn=reduce_fn)
          return x

      model = Foo2()
      variables = model.init(jax.random.PRNGKey(0), x)
      y, state = model.apply(variables, jnp.ones((1, 1)), mutable=['intermediates'])
      print(state['intermediates'])  # ==> {'h': [[3.]]}

    Args:
      col: The name of the variable collection.
      name: The name of the variable.
      value: The value of the variable.
      reduce_fn: The function used to combine the existing value with
        the new value. The default is to append the value to a tuple.
      init_fn: For the first value stored, `reduce_fn` will be passed
        the result of `init_fn` together with the value to be stored.
        The default is an empty tuple.

    Returns:
      `True` if the value has been stored successfully, `False` otherwise.
    """
    if self.scope is None:
      raise ValueError("Can't store variables on unbound modules")
    if not self.scope.is_mutable_collection(col):
      return False
    if self.scope.has_variable(col, name):
      xs = self.scope.get_variable(col, name)
    else:
      self.scope.reserve(name)
      self._state.children[name] = col
      xs = init_fn()
    xs = reduce_fn(xs, value)
    self.scope.put_variable(col, name, xs)
    return True

  def perturb(self, name: str, value: T, collection: str = 'perturbations') -> T:
    """Add an zero-value variable ('perturbation') to the intermediate value.

    The gradient of `value` would be the same as the gradient of this
    perturbation variable. Therefore, if you define your loss function with
    both params and perturbations as standalone arguments, you can get the
    intermediate gradients of `value` by running `jax.grad` on the perturbation
    argument.

    Note: this is an experimental API and may be tweaked later for better
    performance and usability.
    At its current stage, it creates extra dummy variables that occupies extra
    memory space. Use it only to debug gradients in training.

    Example::

      import jax
      import jax.numpy as jnp
      import flax.linen as nn

      class Foo(nn.Module):
          @nn.compact
          def __call__(self, x):
              x = nn.Dense(3)(x)
              x = self.perturb('dense3', x)
              return nn.Dense(2)(x)

      def loss(params, perturbations, inputs, targets):
        variables = {'params': params, 'perturbations': perturbations}
        preds = model.apply(variables, inputs)
        return jnp.square(preds - targets).mean()

      x = jnp.ones((2, 9))
      y = jnp.ones((2, 2))
      model = Foo()
      variables = model.init(jax.random.PRNGKey(0), x)
      intm_grads = jax.grad(loss, argnums=1)(variables['params'], variables['perturbations'], x, y)
      print(intm_grads['dense3']) # ==> [[-1.456924   -0.44332537  0.02422847]
                                  #      [-1.456924   -0.44332537  0.02422847]]

    If perturbations are not passed to `apply`, `perturb` behaves like a no-op
    so you can easily disable the behavior when not needed::

      model.apply({'params': params, 'perturbations': perturbations}, x) # works as expected
      model.apply({'params': params}, x) # behaves like a no-op

    """
    def _root_has_collection():
      """Returns True if the root scope has the collection."""
      assert self.scope is not None
      return collection in self.scope.root._variables
    # we will only add the perturbation variable if the collection is mutable
    # (e.g. during `init`) or if the collection was passed to `apply` (contained in
    # the root scope).
    if self.is_mutable_collection(collection) or _root_has_collection():
      value += self.variable(collection, name, lambda: jnp.zeros_like(value)).value # type: ignore
    return value

  def tabulate(
    self,
    rngs: Union[PRNGKey, RNGSequences],
    *args,
    depth: Optional[int] = None,
    show_repeated: bool = False,
    mutable: CollectionFilter = True,
    console_kwargs: Optional[Mapping[str, Any]] = None,
    **kwargs) -> str:
    """Creates a summary of the Module represented as a table.

    This method has the same signature and internally calls `Module.init`,
    but instead of returning the variables, it returns the string summarizing
    the Module in a table. `tabulate` uses `jax.eval_shape` to run the forward
    computation without consuming any FLOPs or allocating memory.

    Additional arguments can be passed into the `console_kwargs` argument, for example,
    `{'width': 120}`. For a full list of `console_kwargs` arguments, see:
    https://rich.readthedocs.io/en/stable/reference/console.html#rich.console.Console

    Example::

      import jax
      import jax.numpy as jnp
      import flax.linen as nn

      class Foo(nn.Module):
          @nn.compact
          def __call__(self, x):
              h = nn.Dense(4)(x)
              return nn.Dense(2)(h)

      x = jnp.ones((16, 9))

      print(Foo().tabulate(jax.random.PRNGKey(0), x))


    This gives the following output::

                                      Foo Summary
      ┏━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┓
      ┃ path    ┃ module ┃ inputs        ┃ outputs       ┃ params               ┃
      ┡━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━┩
      │         │ Foo    │ float32[16,9] │ float32[16,2] │                      │
      ├─────────┼────────┼───────────────┼───────────────┼──────────────────────┤
      │ Dense_0 │ Dense  │ float32[16,9] │ float32[16,4] │ bias: float32[4]     │
      │         │        │               │               │ kernel: float32[9,4] │
      │         │        │               │               │                      │
      │         │        │               │               │ 40 (160 B)           │
      ├─────────┼────────┼───────────────┼───────────────┼──────────────────────┤
      │ Dense_1 │ Dense  │ float32[16,4] │ float32[16,2] │ bias: float32[2]     │
      │         │        │               │               │ kernel: float32[4,2] │
      │         │        │               │               │                      │
      │         │        │               │               │ 10 (40 B)            │
      ├─────────┼────────┼───────────────┼───────────────┼──────────────────────┤
      │         │        │               │         Total │ 50 (200 B)           │
      └─────────┴────────┴───────────────┴───────────────┴──────────────────────┘

                            Total Parameters: 50 (200 B)

    **Note**: rows order in the table does not represent execution order,
    instead it aligns with the order of keys in `variables` which are sorted
    alphabetically.

    Args:
      rngs: The rngs for the variable collections as passed to `Module.init`.
      *args: The arguments to the forward computation.
      depth: controls how many submodule deep the summary can go. By default its
        `None` which means no limit. If a submodule is not shown because of the
        depth limit, its parameter count and bytes will be added to the row of its
        first shown ancestor such that the sum of all rows always adds up to the
        total number of parameters of the Module.
      show_repeated: If `True`, repeated calls to the same module will be shown
        in the table, otherwise only the first call will be shown. Default is
        `False`.
      mutable: Can be bool, str, or list. Specifies which collections should be
        treated as mutable: ``bool``: all/no collections are mutable. ``str``: The
        name of a single mutable collection. ``list``: A list of names of mutable
        collections. By default all collections except 'intermediates' are
        mutable.
      console_kwargs: An optional dictionary with additional keyword arguments that
        are passed to `rich.console.Console` when rendering the table. Default arguments
        are `{'force_terminal': True, 'force_jupyter': False}`.
      **kwargs: keyword arguments to pass to the forward computation.

    Returns:
      A string summarizing the Module.
    """
    from flax.linen import summary

    tabulate_fn = summary.tabulate(self, rngs, depth=depth,
                                   show_repeated=show_repeated, mutable=mutable,
                                   console_kwargs=console_kwargs)
    return tabulate_fn(*args, **kwargs)


_ParentType = Union[Type[Module], Type[Scope], Type[_Sentinel], None]

def merge_param(name: str, a: Optional[T], b: Optional[T]) -> T:
  """Merges construction- and call-time argument.

  This is a utility for supporting a pattern where a Module hyperparameter
  can be passed either to ``__init__`` or ``__call__``, and the value that is
  not `None` will be used.

  Example::

    class Foo(nn.Module):
      train: Optional[bool] = None

      def __call__(self, train: Optional[bool] = None):
        train = nn.merge_param('train', self.train, train)

  An error is thrown when both arguments are `None` or both values are not
  `None`.

  Args:
    name: the name of the parameter. Used for error messages.
    a: option a
    b: option b
  Returns:
    a or b whichever is not `None`.

  """
  if a is None and b is None:
    raise ValueError(f'Parameter "{name}" must be passed to the constructor or at call time.')
  if a is not None and b is not None:
    raise ValueError(f'Parameter "{name}" was passed to the constructor and at call time.'
                     ' Should be passed just once.')
  if a is None:
    assert b is not None
    return b
  return a


@traceback_util.api_boundary
def apply(fn: Callable[..., Any], module: Module,
          mutable: CollectionFilter = False,
          capture_intermediates: Union[bool, Callable[[Module, str], bool]] = False,
          ) -> Callable[..., Any]:
  """Creates an apply function to call ``fn`` with a bound module.

  Unlike ``Module.apply`` this function returns a new function with the signature
  ``(variables, *args, rngs=None, **kwargs) -> T`` where `T` is the return type
  of ``fn``. If ``mutable`` is not ``False`` the return type is a tuple where the
  second item is a ``FrozenDict`` with the mutated variables.

  The apply function that is returned can be directly composed with
  JAX transformations like ``jax.jit``::

    def f(foo, x):
      z = foo.encode(x)
      y = foo.decode(z)
      # ...
      return y

    foo = Foo()
    f_jitted = jax.jit(nn.apply(f, foo))
    f_jitted(variables, x)

  Args:
    fn: The function that should be applied. The first argument passed will
      be an module instance of the ``module`` with variables and RNGs bound
      to it.
    module: The ``Module`` that will be used to bind variables and RNGs to.
      The ``Module`` passed as the first argument to ``fn`` will be a clone
      of module.
    mutable: Can be bool, str, or list. Specifies which collections should be
      treated as mutable: ``bool``: all/no collections are mutable.
      ``str``: The name of a single mutable collection. ``list``: A
      list of names of mutable collections.
    capture_intermediates: If `True`, captures intermediate return values
      of all Modules inside the "intermediates" collection. By default only
      the return values of all `__call__` methods are stored. A function can
      be passed to change the filter behavior. The filter function takes
      the Module instance and method name and returns a bool indicating
      whether the output of that method invocation should be stored.
  Returns:
    The apply function wrapping ``fn``.
  """
  @functools.wraps(fn)
  def scope_fn(scope, *args, **kwargs):
    _context.capture_stack.append(capture_intermediates)
    try:
      return fn(module.clone(parent=scope), *args, **kwargs)
    finally:
      _context.capture_stack.pop()

  if capture_intermediates is True:  # pylint: disable=g-bool-id-comparison
    capture_intermediates = capture_call_intermediates
  if capture_intermediates:
    mutable = union_filters(mutable, 'intermediates')
  return core.apply(scope_fn, mutable=mutable)


@traceback_util.api_boundary
def init_with_output(fn: Callable[..., Any], module: Module,
                     mutable: CollectionFilter = DenyList('intermediates'),
                     capture_intermediates: Union[bool, Callable[[Module, str], bool]] = False,
                     ) -> Callable[..., Tuple[Any, FrozenVariableDict]]:
  """Creates an init function to call ``fn`` with a bound module that also returns the function outputs.

  Unlike ``Module.init_with_output`` this function returns a new function with the signature
  ``(rngs, *args, **kwargs) -> (T, variables)`` where `T` is the return type of ``fn``.
  The rngs can be a dict of PRNGKeys or a single ```PRNGKey`` which is
  equivalent to passing a dict with one PRNGKey with the name "params".

  The init function that is returned can be directly composed with
  JAX transformations like ``jax.jit``::

    def f(foo, x):
      z = foo.encode(x)
      y = foo.decode(z)
      # ...
      return y

    foo = Foo()
    f_jitted = jax.jit(nn.init_with_output(f, foo))
    y, variables = f_jitted(rng, x)

  Args:
    fn: The function that should be applied. The first argument passed will
      be an module instance of the ``module`` with variables and RNGs bound
      to it.
    module: The ``Module`` that will be used to bind variables and RNGs to.
      The ``Module`` passed as the first argument to ``fn`` will be a clone
      of module.
    mutable: Can be bool, str, or list. Specifies which collections should be
      treated as mutable: ``bool``: all/no collections are mutable.
      ``str``: The name of a single mutable collection. ``list``: A
      list of names of mutable collections. By default all collections
      except "intermediates" are mutable.
    capture_intermediates: If `True`, captures intermediate return values
      of all Modules inside the "intermediates" collection. By default only
      the return values of all `__call__` methods are stored. A function can
      be passed to change the filter behavior. The filter function takes
      the Module instance and method name and returns a bool indicating
      whether the output of that method invocation should be stored.
  Returns:
    The init function wrapping ``fn``.
  """
  @functools.wraps(fn)
  def scope_fn(scope, *args, **kwargs):
    _context.capture_stack.append(capture_intermediates)
    try:
      return fn(module.clone(parent=scope), *args, **kwargs)
    finally:
      _context.capture_stack.pop()

  if capture_intermediates is True:  # pylint: disable=g-bool-id-comparison
    capture_intermediates = capture_call_intermediates
  if capture_intermediates:
    mutable = union_filters(mutable, 'intermediates')
  return core.init(scope_fn, mutable=mutable)


@traceback_util.api_boundary
def init(fn: Callable[..., Any], module: Module,
         mutable: CollectionFilter = DenyList('intermediates'),
         capture_intermediates: Union[bool, Callable[[Module, str], bool]] = False,
         ) -> Callable[..., FrozenVariableDict]:
  """Creates an init function to call ``fn`` with a bound module.

  Unlike ``Module.init`` this function returns a new function with the signature
  ``(rngs, *args, **kwargs) -> variables``.
  The rngs can be a dict of PRNGKeys or a single ```PRNGKey`` which is
  equivalent to passing a dict with one PRNGKey with the name "params".

  The init function that is returned can be directly composed with
  JAX transformations like ``jax.jit``::

    def f(foo, x):
      z = foo.encode(x)
      y = foo.decode(z)
      # ...
      return y

    foo = Foo()
    f_jitted = jax.jit(nn.init(f, foo))
    variables = f_jitted(rng, x)

  Args:
    fn: The function that should be applied. The first argument passed will
      be an module instance of the ``module`` with variables and RNGs bound
      to it.
    module: The ``Module`` that will be used to bind variables and RNGs to.
      The ``Module`` passed as the first argument to ``fn`` will be a clone
      of module.
    mutable: Can be bool, str, or list. Specifies which collections should be
      treated as mutable: ``bool``: all/no collections are mutable.
      ``str``: The name of a single mutable collection. ``list``: A
      list of names of mutable collections. By default all collections
      except "intermediates" are mutable.
    capture_intermediates: If `True`, captures intermediate return values
      of all Modules inside the "intermediates" collection. By default only
      the return values of all `__call__` methods are stored. A function can
      be passed to change the filter behavior. The filter function takes
      the Module instance and method name and returns a bool indicating
      whether the output of that method invocation should be stored.
  Returns:
    The init function wrapping ``fn``.
  """
  init_fn = init_with_output(fn, module, mutable, capture_intermediates)
  @functools.wraps(init_fn)
  def init_wrapper(*args, **kwargs):
    return init_fn(*args, **kwargs)[1]
  return init_wrapper

