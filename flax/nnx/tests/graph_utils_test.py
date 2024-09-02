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

import dataclasses
from functools import partial
from threading import Thread
from typing import Any

from absl.testing import absltest
from flax import linen, nnx, struct
import jax
import jax.numpy as jnp
import pytest


class StatefulLinear(nnx.Module):
  def __init__(self, din, dout, rngs):
    self.w = nnx.Param(jax.random.uniform(rngs(), (din, dout)))
    self.b = nnx.Param(jnp.zeros((dout,)))
    self.count = nnx.Variable(jnp.array(0, dtype=jnp.uint32))

  def increment(self):
    self.count.value += 1

  def __call__(self, x):
    self.count.value += 1
    return x @ self.w + self.b[None]


class TestGraphUtils(absltest.TestCase):
  def test_flatten(self):
    a = {'a': 1, 'b': nnx.Param(2)}
    g = [a, 3, a, nnx.Param(4)]

    refmap = nnx.graph.RefMap()
    graphdef, state = nnx.graph.flatten(g, ref_index=refmap)

    state[0]['b'].raw_value = 2
    state[3].raw_value = 4

    assert len(refmap) == 2
    assert a['b'] in refmap
    assert g[3] in refmap

  def test_unflatten(self):
    a = nnx.Dict(a=1, b=nnx.Param(2))
    g = nnx.List([a, 3, a, nnx.Param(4)])

    graphdef, state = nnx.split(g)
    g = nnx.merge(graphdef, state)

    assert g[0] is g[2]

  def test_unflatten_pytree(self):
    a = {'a': 1, 'b': nnx.Param(2)}
    g = [a, 3, a, nnx.Param(4)]

    graphdef, state = nnx.split(g)
    g = nnx.merge(graphdef, state)

    assert g[0] is not g[2]

  def test_unflatten_empty(self):
    a = nnx.Dict({'a': 1, 'b': nnx.Param(2)})
    g = nnx.List([a, 3, a, nnx.Param(4)])

    graphdef, state = nnx.split(g)

    with pytest.raises(ValueError, match='Expected key'):
      nnx.graph.unflatten(graphdef, nnx.State({}))

  def test_update_dynamic(self):
    a = {'a': 1, 'b': nnx.Param(2)}
    g = [a, 3, a, nnx.Param(4)]

    graphdef, state = nnx.split(g)

    state[0]['b'].value = 3
    nnx.graph.update(g, state)

    assert g[0]['b'].value == 3
    assert g[2]['b'].value == 3

  def test_update_static(self):
    a = nnx.Dict({'a': 1, 'b': nnx.Param(2)})
    g = nnx.List([a, 3, a, nnx.Param(4)])

    g2 = nnx.graph.clone(g)
    g2[0]['a'] = 5

    nnx.graph.graph_update_static(g, g2)

    assert g[0]['a'] == 5
    assert g[2]['a'] == 5

  def test_update_static_inconsistent_types(self):
    a = {'a': 1, 'b': nnx.Param(2)}
    g = [a, 3, a, nnx.Param(4)]
    g2 = [a, a, 3, nnx.Param(4)]

    with pytest.raises(
      ValueError, match='Trying to update a node with a different type'
    ):
      nnx.graph.graph_update_static(g, g2)

  def test_update_static_add_new(self):
    a = nnx.Dict({'a': 1, 'b': nnx.Param(2)})
    b = nnx.List([5, 6])
    g = nnx.List([a, 3, a, nnx.Param(4)])
    g2 = nnx.List([a, 3, a, nnx.Param(4), b])

    nnx.graph.graph_update_static(g, g2)

    assert g[4][0] == 5
    assert g[4][1] == 6

  def test_update_static_add_shared_error(self):
    a = nnx.Dict({'a': 1, 'b': nnx.Param(2)})
    g = nnx.List([a, 3, a, nnx.Param(4)])
    g2 = nnx.List([a, 3, a, nnx.Param(4), a])

    with pytest.raises(ValueError, match='Trying to add a new node at path'):
      nnx.graph.graph_update_static(g, g2)

  def test_module_list(self):
    rngs = nnx.Rngs(0)
    ls = [
      nnx.Linear(2, 2, rngs=rngs),
      nnx.BatchNorm(2, rngs=rngs),
    ]

    graphdef, state = nnx.split(ls)

    assert state[0]['kernel'].value.shape == (2, 2)
    assert state[0]['bias'].value.shape == (2,)
    assert state[1]['scale'].value.shape == (2,)
    assert state[1]['bias'].value.shape == (2,)
    assert state[1]['mean'].value.shape == (2,)
    assert state[1]['var'].value.shape == (2,)

  def test_shared_variables(self):
    v = nnx.Param(1)
    g = [v, v]

    graphdef, state = nnx.split(g)

    assert len(state.flat_state()) == 1

    g2 = nnx.merge(graphdef, state)

    assert g2[0] is g2[1]

  def test_tied_weights(self):
    class Foo(nnx.Module):
      def __init__(self, *, rngs: nnx.Rngs) -> None:
        self.bar = nnx.Linear(2, 2, rngs=rngs)
        self.baz = nnx.Linear(2, 2, rngs=rngs)

        # tie the weights
        self.baz.kernel = self.bar.kernel

    node = Foo(rngs=nnx.Rngs(0))
    graphdef, state = nnx.split(node)

    assert len(state.flat_state()) == 3  # 2 bias + 1 kernel

    node2 = nnx.merge(graphdef, state)

    assert node2.bar.kernel is node2.baz.kernel

  def test_tied_weights_example(self):
    class LinearTranspose(nnx.Module):
      def __init__(self, dout: int, din: int, *, rngs: nnx.Rngs) -> None:
        self.kernel = nnx.Param(
          nnx.initializers.lecun_normal()(rngs(), (dout, din))
        )

      def __call__(self, x):
        return x @ self.kernel.value.T

    class Encoder(nnx.Module):
      def __init__(self, *, rngs: nnx.Rngs) -> None:
        self.embed = nnx.Embed(10, 2, rngs=rngs)
        ...
        self.linear_out = LinearTranspose(10, 2, rngs=rngs)

        # tie the weights
        self.linear_out.kernel = self.embed.embedding

      def __call__(self, x):
        x = self.embed(x)
        ...
        return self.linear_out(x)

    model = Encoder(rngs=nnx.Rngs(0))
    graphdef, state = nnx.split(model)

    assert len(state.flat_state()) == 1

    x = jax.random.randint(jax.random.key(0), (2,), 0, 10)
    y = model(x)

    assert y.shape == (2, 10)

  def test_state_variables_not_shared_with_graph(self):
    class Foo(nnx.Module):
      def __init__(self):
        self.a = nnx.Param(1)

    m = Foo()
    graphdef, state = nnx.split(m)

    assert isinstance(m.a, nnx.Param)
    assert issubclass(state.a.type, nnx.Param)
    assert m.a is not state.a
    assert m.a.value == state.a.value

    m2 = nnx.merge(graphdef, state)

    assert isinstance(m2.a, nnx.Param)
    assert issubclass(state.a.type, nnx.Param)
    assert m2.a is not state.a
    assert m2.a.value == state.a.value

  def test_shared_state_variables_not_shared_with_graph(self):
    class Foo(nnx.Module):
      def __init__(self):
        p = nnx.Param(1)
        self.a = p
        self.b = p

    m = Foo()
    graphdef, state = nnx.split(m)

    assert isinstance(m.a, nnx.Param)
    assert isinstance(m.b, nnx.Param)
    assert issubclass(state.a.type, nnx.Param)
    assert 'b' not in state
    assert m.a is not state.a
    assert m.b is not state.a
    assert m.a.value == state.a.value
    assert m.b.value == state.a.value

    m2 = nnx.merge(graphdef, state)

    assert isinstance(m2.a, nnx.Param)
    assert isinstance(m2.b, nnx.Param)
    assert issubclass(state.a.type, nnx.Param)
    assert m2.a is not state.a
    assert m2.b is not state.a
    assert m2.a.value == state.a.value
    assert m2.b.value == state.a.value
    assert m2.a is m2.b

  def test_pytree_flatten(self):
    @struct.dataclass
    class Tree:
      a: int
      b: str = struct.field(pytree_node=False)

    p = Tree(1, 'a')

    leaves, treedef = nnx.graph._flatten_pytree(p)
    fields = dict(leaves)

    assert 'a' in fields
    assert 'b' not in fields
    assert fields['a'] == 1

    p2 = nnx.graph._unflatten_pytree(leaves, treedef)

    assert isinstance(p2, Tree)
    assert p2.a == 1

  def test_pytree_node(self):
    @struct.dataclass
    class Tree:
      a: nnx.Param[int]
      b: str = struct.field(pytree_node=False)

    class Foo(nnx.Module):
      def __init__(self):
        self.tree = Tree(nnx.Param(1), 'a')

    m = Foo()

    graphdef, state = nnx.split(m)

    assert 'tree' in state
    assert 'a' in state.tree
    assert graphdef.subgraphs['tree'].type is nnx.graph.PytreeType

    m2 = nnx.merge(graphdef, state)

    assert isinstance(m2.tree, Tree)
    assert m2.tree.a.raw_value == 1
    assert m2.tree.b == 'a'
    assert m2.tree.a is not m.tree.a
    assert m2.tree is not m.tree

  def test_cached_unflatten(self):
    class Foo(nnx.Module):
      def __init__(self, *, rngs: nnx.Rngs):
        self.a = nnx.Linear(2, 2, rngs=rngs)
        self.b = nnx.BatchNorm(2, rngs=rngs)

    def f(m: Foo):
      m.a, m.b = m.b, m.a  # type: ignore

    m = Foo(rngs=nnx.Rngs(0))
    a = m.a
    b = m.b

    ref_out_idx_out = nnx.graph.RefMap()
    graphdef: nnx.graph.GraphDef[Foo]
    graphdef, state = nnx.graph.flatten(m, ref_index=ref_out_idx_out)

    @partial(jax.jit, static_argnums=(0,))
    def f_pure(graphdef: nnx.graph.GraphDef[Foo], state):
      idx_out_ref_in: dict[int, Any] = {}
      m = nnx.graph.unflatten(graphdef, state, index_ref=idx_out_ref_in)
      f(m)
      ref_in_idx_in = nnx.graph.RefMap[Any, int]()
      graphdef, state = nnx.graph.flatten(m, ref_index=ref_in_idx_in)
      idx_out_idx_in = nnx.graph.compose_mapping(idx_out_ref_in, ref_in_idx_in)
      static_out = nnx.graph.Static((graphdef, idx_out_idx_in))
      return state, static_out

    static_out: nnx.graph.Static
    state, static_out = f_pure(graphdef, state)
    idx_out_idx_in: dict[int, int]
    graphdef, idx_out_idx_in = static_out.value
    idx_in_ref_out = nnx.graph.compose_mapping_reversed(
      ref_out_idx_out, idx_out_idx_in
    )
    m2 = nnx.graph.unflatten(graphdef, state, index_ref_cache=idx_in_ref_out)
    assert m2 is m
    assert m2.a is b
    assert m2.b is a

  def test_cached_unflatten_swap_variables(self):
    class Foo(nnx.Module):
      def __init__(self):
        self.a = nnx.Param(1)
        self.b = nnx.Param(2)

    def f(m: Foo):
      m.a, m.b = m.b, m.a

    m = Foo()
    a = m.a
    b = m.b

    ref_out_idx_out = nnx.graph.RefMap[Any, int]()
    graphdef: nnx.graph.GraphDef[Foo]
    graphdef, state = nnx.graph.flatten(m, ref_index=ref_out_idx_out)

    @partial(jax.jit, static_argnums=(0,))
    def f_pure(graphdef: nnx.graph.GraphDef[Foo], state):
      idx_out_ref_in: dict[int, Any] = {}
      m = nnx.graph.unflatten(graphdef, state, index_ref=idx_out_ref_in)
      f(m)
      ref_in_idx_in = nnx.graph.RefMap[Any, int]()
      graphdef, state = nnx.graph.flatten(m, ref_index=ref_in_idx_in)
      idx_out_idx_in = nnx.graph.compose_mapping(idx_out_ref_in, ref_in_idx_in)
      static_out = nnx.graph.Static((graphdef, idx_out_idx_in))
      return state, static_out

    static_out: nnx.graph.Static
    state, static_out = f_pure(graphdef, state)
    idx_out_idx_in: dict[int, int]
    graphdef, idx_out_idx_in = static_out.value
    idx_in_ref_out = nnx.graph.compose_mapping_reversed(
      ref_out_idx_out, idx_out_idx_in
    )
    m2 = nnx.graph.unflatten(graphdef, state, index_ref_cache=idx_in_ref_out)
    assert m2 is m
    assert m2.a is b
    assert m2.b is a

  def test_cached_unflatten_add_self_reference(self):
    class Foo(nnx.Module):
      def __init__(self):
        self.ref = None

    def f(m: Foo):
      m.ref = m

    m = Foo()

    ref_out_idx_out = nnx.graph.RefMap()
    graphdef: nnx.graph.GraphDef[Foo]
    graphdef, state = nnx.graph.flatten(m, ref_index=ref_out_idx_out)

    @partial(jax.jit, static_argnums=(0,))
    def f_pure(graphdef: nnx.graph.GraphDef[Foo], state):
      idx_out_ref_in: dict[int, Any] = {}
      m = nnx.graph.unflatten(graphdef, state, index_ref=idx_out_ref_in)
      f(m)
      ref_in_idx_in = nnx.graph.RefMap[Any, int]()
      graphdef, state = nnx.graph.flatten(m, ref_index=ref_in_idx_in)
      idx_out_idx_in = nnx.graph.compose_mapping(idx_out_ref_in, ref_in_idx_in)
      static_out = nnx.graph.Static((graphdef, idx_out_idx_in))
      return state, static_out

    static_out: nnx.graph.Static
    state, static_out = f_pure(graphdef, state)
    idx_out_idx_in: dict[int, int]
    graphdef, idx_out_idx_in = static_out.value
    idx_in_ref_out = nnx.graph.compose_mapping_reversed(
      ref_out_idx_out, idx_out_idx_in
    )
    m2 = nnx.graph.unflatten(graphdef, state, index_ref_cache=idx_in_ref_out)
    assert m2 is m
    assert m2.ref is m2

  def test_object_state_propagation(self):
    test = self

    class Foo(nnx.Module):
      def __call__(self):
        test.assertTrue(self._object__state.initializing)
        self = nnx.merge(*nnx.split(self))
        test.assertTrue(self._object__state.initializing)

    module = Foo()
    nnx.bridge.lazy_init(module)

  def test_object_state_propagation_nested(self):
    class NNXOuter(nnx.Module):
      def __init__(self, dout: int, rngs: nnx.Rngs):
        self.inner = nnx.bridge.ToNNX(linen.Dense(dout), rngs=rngs)
        self.rngs = rngs

      def __call__(self, x):
        @nnx.split_rngs(splits=5)
        @nnx.vmap(in_axes=(0, None), axis_size=5)
        def vmap_fn(inner, x):
          return inner(x)

        return vmap_fn(self.inner, x)

    x = jax.random.normal(jax.random.key(0), (2, 4))
    model = NNXOuter(3, rngs=nnx.Rngs(0))
    nnx.bridge.lazy_init(model, x)

    self.assertEqual(model.inner.params['kernel'].shape, (5, 4, 3))
    self.assertEqual(model.inner.params['bias'].shape, (5, 3))

  def test_split_merge_context(self):
    m = nnx.Linear(2, 3, rngs=nnx.Rngs(0))
    with nnx.graph.split_context() as ctx:
      graphdef1, state1 = ctx.split(m)
      graphdef2, state2 = ctx.split(m)

    self.assertFalse(hasattr(ctx, 'ref_index'))
    self.assertFalse(hasattr(ctx, 'ctxtag'))
    self.assertIsInstance(graphdef1, nnx.graph.NodeDef)
    self.assertIsInstance(graphdef2, nnx.graph.NodeRef)
    self.assertLen(state1.flat_state(), 2)
    self.assertLen(state2.flat_state(), 0)

    with nnx.graph.merge_context() as ctx:
      m1 = ctx.merge(graphdef1, state1)
      m2 = ctx.merge(graphdef2, state2)

    self.assertIs(m1, m2)
    self.assertFalse(hasattr(ctx, 'index_ref'))
    self.assertFalse(hasattr(ctx, 'ctxtag'))

  def test_split_merge_context_nested(self):
    m2 = nnx.Linear(2, 3, rngs=nnx.Rngs(0))
    m1 = nnx.Sequential(m2)
    with nnx.graph.split_context() as ctx:
      graphdef1, state1 = ctx.split(m1)
      graphdef2, state2 = ctx.split(m2)

    self.assertIsInstance(graphdef1, nnx.graph.NodeDef)
    self.assertIsInstance(graphdef2, nnx.graph.NodeRef)
    self.assertLen(state1.flat_state(), 2)
    self.assertLen(state2.flat_state(), 0)

    with nnx.graph.merge_context() as ctx:
      m1 = ctx.merge(graphdef1, state1)
      m2 = ctx.merge(graphdef2, state2)

    self.assertIs(m2, m1.layers[0])
    self.assertFalse(hasattr(ctx, 'index_ref'))
    self.assertFalse(hasattr(ctx, 'ctxtag'))

  def test_split_merge_update_context(self):
    class Foo(nnx.Module):

      def __init__(self):
        self.a = nnx.Param(1)
        self.b = 2

    m = Foo()
    ctxtag = 'test'

    with nnx.update_context(ctxtag):
      with nnx.graph.split_context(ctxtag) as ctx:
        graphdef1, state1 = ctx.split(m)
        graphdef2, state2 = ctx.split(m)

      self.assertFalse(hasattr(ctx, 'ref_index'))
      self.assertFalse(hasattr(ctx, 'ctxtag'))
      self.assertIsInstance(graphdef1, nnx.graph.NodeDef)
      self.assertIsInstance(graphdef2, nnx.graph.NodeRef)
      self.assertLen(state1.flat_state(), 1)
      self.assertLen(state2.flat_state(), 0)

      @jax.jit
      def f(graphdef1, state1, graphdef2, state2):
        with nnx.graph.merge_context(ctxtag) as ctx:
          m1 = ctx.merge(graphdef1, state1)
          m2 = ctx.merge(graphdef2, state2)

        self.assertIs(m1, m2)
        self.assertFalse(hasattr(ctx, 'index_ref'))
        self.assertFalse(hasattr(ctx, 'ctxtag'))

        # swap a and b
        m1.a, m1.b = m1.b, m1.a

        with nnx.graph.split_context(ctxtag) as ctx:
          graphdef1, state1 = ctx.split(m1)
          graphdef2, state2 = ctx.split(m2)

        return graphdef1, state1, graphdef2, state2

      graphdef1, state1, graphdef2, state2 = f(
          graphdef1, state1, graphdef2, state2
      )

      with nnx.graph.merge_context(ctxtag) as ctx:
        m1_out = ctx.merge(graphdef1, state1)
        m2_out = ctx.merge(graphdef2, state2)

      self.assertIs(m, m1_out)
      self.assertIs(m, m2_out)
      self.assertEqual(m.a, 2)
      self.assertEqual(m.b.value, 1)  # type: ignore

      self.assertFalse(hasattr(ctx, 'index_ref'))
      self.assertFalse(hasattr(ctx, 'ctxtag'))

  def test_to_tree_simple(self):
    m = nnx.Linear(2, 3, rngs=nnx.Rngs(0))
    impure_tree = (m, 1, {'b': m})

    pure_tree = nnx.to_tree(impure_tree)

    t1 = pure_tree[0]
    t2 = pure_tree[2]['b']

    self.assertEqual(pure_tree[1], 1)
    self.assertIsInstance(t1, nnx.TreeNode)
    assert isinstance(t1, nnx.TreeNode)
    self.assertIsInstance(t2, nnx.TreeNode)
    assert isinstance(t2, nnx.TreeNode)
    self.assertIsInstance(t1.graphdef, nnx.graph.NodeDef)
    self.assertIsInstance(t2.graphdef, nnx.graph.NodeRef)
    self.assertLen(t1.states[0].flat_state(), 2)
    self.assertLen(t2.states[0].flat_state(), 0)

    impure_tree2 = nnx.from_tree(pure_tree)

    m1_out = impure_tree2[0]
    m2_out = impure_tree2[2]['b']

    self.assertIs(m1_out, m2_out)
    self.assertEqual(impure_tree2[1], 1)

  def test_to_tree_update_context(self):
    class Foo(nnx.Module):

      def __init__(self):
        self.a = nnx.Param(1)
        self.b = 2

    m = Foo()
    impure_tree = (m, 1, {'b': m})
    ctxtag = 'test'

    with nnx.update_context(ctxtag):
      pure_tree = nnx.to_tree(impure_tree, ctxtag=ctxtag)

      t1 = pure_tree[0]
      t2 = pure_tree[2]['b']

      self.assertEqual(pure_tree[1], 1)
      self.assertIsInstance(t1, nnx.TreeNode)
      assert isinstance(t1, nnx.TreeNode)
      self.assertIsInstance(t2, nnx.TreeNode)
      assert isinstance(t2, nnx.TreeNode)
      self.assertIsInstance(t1.graphdef, nnx.graph.NodeDef)
      self.assertIsInstance(t2.graphdef, nnx.graph.NodeRef)
      self.assertLen(t1.states[0].flat_state(), 1)
      self.assertLen(t2.states[0].flat_state(), 0)

      @jax.jit
      def f(pure_tree):
        impure_tree2 = nnx.from_tree(pure_tree, ctxtag=ctxtag)
        m1_out = impure_tree2[0]
        m2_out = impure_tree2[2]['b']

        self.assertIs(m1_out, m2_out)
        # self.assertEqual(impure_tree2[1], 1)

        # swap a and b
        m1_out.a, m1_out.b = m1_out.b, m1_out.a

        pure_tree2 = nnx.to_tree(impure_tree2, ctxtag=ctxtag)

        t1 = pure_tree2[0]
        t2 = pure_tree2[2]['b']

        # self.assertEqual(pure_tree2[1], 1)
        self.assertIsInstance(t1, nnx.TreeNode)
        assert isinstance(t1, nnx.TreeNode)
        self.assertIsInstance(t2, nnx.TreeNode)
        assert isinstance(t2, nnx.TreeNode)
        self.assertIsInstance(t1.graphdef, nnx.graph.NodeDef)
        self.assertIsInstance(t2.graphdef, nnx.graph.NodeRef)
        self.assertLen(t1.states[0].flat_state(), 1)
        self.assertLen(t2.states[0].flat_state(), 0)

        return pure_tree2

      pure_tree2 = f(pure_tree)

      impure_tree2 = nnx.from_tree(pure_tree2, ctxtag=ctxtag)

      m1_out = impure_tree2[0]
      m2_out = impure_tree2[2]['b']

      self.assertIs(m, m1_out)
      self.assertIs(m, m2_out)
      self.assertEqual(m.a, 2)
      self.assertEqual(m.b.value, 1)  # type: ignore
      self.assertEqual(impure_tree2[1], 1)

  def test_to_tree_consistent_prefix(self):
    m = nnx.Linear(2, 3, rngs=nnx.Rngs(0))
    impure_tree = (m, 1, {'b': m})
    prefix = (0, None, 0)
    pure_tree = nnx.to_tree(impure_tree, prefix=prefix)

    prefix = (0, None, 1)
    with pytest.raises(ValueError, match='Inconsistent aliasing detected'):
      nnx.to_tree(impure_tree, prefix=prefix)

  def test_simple_vmap(self):
    @dataclasses.dataclass(frozen=True)
    class StateAxes:
      params: Any
      batch_stats: Any

    class Foo(nnx.Module):

      def __init__(self, a, b):
        self.a = nnx.Param(a)
        self.b = nnx.BatchStat(b)

    ctxtag = 'test'
    with nnx.update_context(ctxtag):
      m1 = Foo(a=jnp.array(0), b=jnp.arange(5))
      m2 = Foo(a=jnp.array(1), b=jnp.array(2))

      args = (m1, m2, {'b': m1})
      m1_axes = StateAxes(None, 0)
      in_axes = (m1_axes, None, {'b': m1_axes})
      jax_in_axes = jax.tree.map(
          lambda x: nnx.TreeNode.from_prefixes((x.params, x.batch_stats))
          if isinstance(x, StateAxes)
          else x,
          in_axes,
      )
      out_axes = 0

      def split_fn(ctx: nnx.SplitContext, path, prefix, x):
        if isinstance(prefix, StateAxes):
          return nnx.TreeNode.from_split(
              *ctx.split(x, nnx.Param, nnx.BatchStat)
          )
        return nnx.TreeNode.from_split(*ctx.split(x))

      pure_args = nnx.to_tree(
          args, ctxtag=ctxtag, prefix=in_axes, split_fn=split_fn
      )

      @partial(jax.vmap, in_axes=jax_in_axes, out_axes=(jax_in_axes, out_axes))
      def f(*pure_args):
        args = nnx.from_tree(pure_args, ctxtag=ctxtag)

        y = 0

        self.assertIs(args[0], args[2]['b'])
        for path, m in nnx.iter_graph(args):
          if isinstance(m, Foo):
            self.assertEqual(m.a.shape, ())
            self.assertEqual(m.b.shape, ())
            y += m.a + m.b

        args_out = nnx.extract.clear_non_graph_nodes(args)

        pure_args_out, y = nnx.to_tree(
            (args_out, y),
            prefix=(in_axes, out_axes),
            ctxtag=ctxtag,
            split_fn=split_fn,
        )
        return pure_args_out, y

      pure_args_out, y = f(*pure_args)

      args_out, y = nnx.from_tree((pure_args_out, y), ctxtag=ctxtag)

    self.assertEqual(y.shape, (5,))
    self.assertGreater(y.sum(), 5)
    self.assertIs(m1, args_out[0])
    self.assertIs(m1, args_out[2]['b'])
    self.assertIs(m2, args_out[1])


class SimpleModule(nnx.Module):
  pass


class SimplePyTreeModule(nnx.Module, experimental_pytree=True):
  pass


@pytest.mark.parametrize(['x'], [(SimpleModule(),), (SimplePyTreeModule(),)])
def test_threading(x: nnx.Module):
  class MyThread(Thread):
    def run(self) -> None:
      nnx.graph.split(x)

  thread = MyThread()
  thread.start()
  thread.join()
