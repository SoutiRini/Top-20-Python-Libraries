# Copyright 2020 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import threading

from tensorflow.python.distribute.parallel_device import parallel_device
from tensorflow.python.eager import backprop
from tensorflow.python.eager import context
from tensorflow.python.eager import def_function
from tensorflow.python.framework import config
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import ops
from tensorflow.python.module import module
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import collective_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import variables
from tensorflow.python.platform import test
from tensorflow.python.training import checkpoint_management
from tensorflow.python.training.tracking import util as tracking
from tensorflow.python.util import nest

# When running collectives asynchronously, we need to give each parallel device
# execution a unique ID so the collectives don't interfere. Since the op is
# replicated with group/instance key intact, the replicated nodes will
# communicate.
# TODO(allenl): Switch to using a collective manager.
_COUNTER_LOCK = threading.Lock()
_COUNTER = 100


def _collective_reduce(inputs, operation, num_replicas):

  def _reduce_tensor(tensor):
    with _COUNTER_LOCK:
      global _COUNTER
      keys = _COUNTER
      _COUNTER += 1
    return collective_ops.all_reduce(
        t=tensor,
        group_size=num_replicas,
        merge_op=operation,
        group_key=keys,
        instance_key=keys,
        final_op="Id")

  return nest.map_structure(_reduce_tensor, inputs)


def _collective_sum(inputs, num_replicas):
  return _collective_reduce(
      inputs=inputs, operation="Add", num_replicas=num_replicas)


class _Dense(module.Module):

  def __init__(self, output_size):
    self.output_size = output_size
    self.kernel = None
    self.bias = None

  def __call__(self, x):
    if self.kernel is None:
      self.kernel = variables.Variable(
          array_ops.ones(
              array_ops.stack([self.output_size,
                               array_ops.shape(x)[-1]])))
      self.bias = variables.Variable(array_ops.ones([self.output_size]))
    return math_ops.matmul(x, self.kernel, transpose_b=True) + self.bias


class _VirtualDeviceTestCase(test.TestCase):

  def setUp(self):
    super(_VirtualDeviceTestCase, self).setUp()
    cpus = context.context().list_physical_devices("CPU")
    # Set 4 virtual CPUs
    context.context().set_logical_device_configuration(cpus[0], [
        context.LogicalDeviceConfiguration(),
        context.LogicalDeviceConfiguration(),
        context.LogicalDeviceConfiguration(),
        context.LogicalDeviceConfiguration()
    ])

    # TODO(allenl): Make CPU:0 and CPU:1 work (right now "CPU:1" soft-places
    # onto CPU:0, which seems wrong).
    components = [
        "/job:localhost/replica:0/task:0/device:CPU:0",
        "/job:localhost/replica:0/task:0/device:CPU:1"
    ]
    self.device = parallel_device.ParallelDevice(components)


class ParallelDeviceTests(_VirtualDeviceTestCase):

  def test_register_parallel_device(self):
    with ops.device(self.device.name):
      c = constant_op.constant(1.)
      d = constant_op.constant(2.)
      e = c + d
      outputs = self.device.unpack(e)
    self.assertAllClose([3., 3.], outputs)

    self.assertIn(self.device.components[0], outputs[0].backing_device)
    self.assertIn(self.device.components[1], outputs[1].backing_device)

  def test_device_id(self):
    device_ids = self.device.unpack(self.device.device_ids)
    self.assertAllClose([0, 1], device_ids)
    self.assertIn(self.device.components[0], device_ids[0].backing_device)
    self.assertIn(self.device.components[1], device_ids[1].backing_device)

  def test_collective_reduce(self):
    with ops.device(self.device.name):
      x = self.device.pack(
          [constant_op.constant(-1.5),
           constant_op.constant(3.5)])
      reduced = _collective_sum(x, num_replicas=2)
      outputs = self.device.unpack(reduced)
    self.assertAllClose([2., 2.], outputs)
    self.assertIn(self.device.components[0], outputs[0].backing_device)
    self.assertIn(self.device.components[1], outputs[1].backing_device)

  def test_collective_reduce_async_scope(self):
    # Note that ops on the parallel device currently don't execute
    # asynchronously. The test is just that we don't get deadlocks.
    with context.async_scope(), ops.device(self.device.name):
      x = self.device.pack(
          [constant_op.constant(-1.5),
           constant_op.constant(3.5)])
      reduced = _collective_sum(x, num_replicas=2)
      outputs = self.device.unpack(reduced)
    self.assertAllClose([2., 2.], outputs)
    self.assertIn(self.device.components[0], outputs[0].backing_device)
    self.assertIn(self.device.components[1], outputs[1].backing_device)

  def test_collective_reduce_async_context(self):
    previous = config.get_synchronous_execution()
    try:
      context._reset_context()
      config.set_synchronous_execution(False)
      self.setUp()
      # Note that ops on the parallel device currently don't execute
      # asynchronously. The test is just that we don't get deadlocks.
      with ops.device(self.device.name):
        x = self.device.pack(
            [constant_op.constant(-1.5),
             constant_op.constant(3.5)])
        reduced = _collective_sum(x, num_replicas=2)
        outputs = self.device.unpack(reduced)
      self.assertAllClose([2., 2.], outputs)
      self.assertIn(self.device.components[0], outputs[0].backing_device)
      self.assertIn(self.device.components[1], outputs[1].backing_device)
    finally:
      context._reset_context()
      config.set_synchronous_execution(previous)

  def test_collective_in_function(self):
    c = constant_op.constant([2])

    @def_function.function
    def broadcast_send_recv(device_id):

      @def_function.function
      def send():
        s0 = collective_ops.broadcast_send(
            c * 3, c.shape, c.dtype, group_size=2, group_key=1, instance_key=1)
        with ops.control_dependencies([s0.op]):
          return array_ops.identity(c)

      @def_function.function
      def recv():
        r0 = collective_ops.broadcast_recv(
            c.shape, c.dtype, group_size=2, group_key=1, instance_key=1)
        return r0

      return control_flow_ops.switch_case(
          device_id, branch_fns={0: send, 1: recv})

    with ops.device(self.device.name):
      result = broadcast_send_recv(self.device.device_ids)
    self.assertAllClose([[2], [6]], self.device.unpack(result))

  def test_checkpointing(self):
    self.skipTest(
        "Disable saving until SaveableObject's methods are traceable.")
    prefix = os.path.join(self.get_temp_dir(), "ckpt")
    with self.device.scope():
      different_values = self.device.pack(
          [constant_op.constant(-1.),
           constant_op.constant(3.)])
      v = variables.Variable(different_values)
      checkpoint = tracking.Checkpoint(v=v)
    save_path = checkpoint.save(prefix)
    with ops.device(self.device.name):
      v.assign(constant_op.constant(0.))
    checkpoint.restore(save_path).assert_consumed()
    with ops.device(self.device.name):
      outputs = self.device.unpack(v)
    self.assertAllClose([-1., 3.], outputs)

  def _assert_close_to_non_parallel(self, computation):
    """Asserts that replication of `computation` works and is equivalent."""
    with ops.device(self.device.name):
      parallel_result = computation()
    non_parallel_result = computation()
    # The computations should have the same number and structure of Tensor
    # objects, even though the tensors themselves will be on different devices
    # and represent different numbers of values.
    nest.assert_same_structure(parallel_result, non_parallel_result)
    non_parallel_flat = nest.flatten(non_parallel_result)
    parallel_flat = nest.flatten(parallel_result)
    self.assertGreater(len(parallel_flat), 0)
    for non_parallel, parallel in zip(non_parallel_flat, parallel_flat):
      self.assertEqual(self.device.name, parallel.device)
      self.assertNotEqual(self.device.name, non_parallel.device)
      for parallel_component in self.device.unpack(parallel):
        self.assertAllClose(non_parallel, parallel_component)

  def test_euclidean_norm(self):
    def _test_fn():
      with backprop.GradientTape() as tape:
        x = array_ops.ones([5, 5])
        tape.watch(x)
        y = math_ops.reduce_euclidean_norm(x, axis=constant_op.constant(1))
      return y, tape.gradient(y, x)
    self._assert_close_to_non_parallel(_test_fn)

  def test_reduce_sum(self):
    def _test_fn():
      with backprop.GradientTape() as tape:
        x = array_ops.ones([5, 5])
        tape.watch(x)
        y = math_ops.reduce_sum(x, axis=constant_op.constant(1))
      return y, tape.gradient(y, x)
    self._assert_close_to_non_parallel(_test_fn)


class LayerTests(_VirtualDeviceTestCase):

  def test_layer_forward(self):
    with ops.device(self.device.name):
      layer = _Dense(5)
      x = constant_op.constant([[2.]])
      y = layer(x)
      outputs = self.device.unpack(y)
    self.assertAllClose([[3.] * 5], outputs[0])
    self.assertAllClose([[3.] * 5], outputs[1])
    self.assertIn(self.device.components[0], outputs[0].backing_device)
    self.assertIn(self.device.components[1], outputs[1].backing_device)

    # With different Layer inputs we get different outputs
    with ops.device(self.device.name):
      x = self.device.pack(
          [constant_op.constant([[-0.5]]),
           constant_op.constant([[0.5]])])
      y = layer(x)
      outputs = self.device.unpack(y)
    self.assertGreater(
        math_ops.reduce_max(math_ops.abs(outputs[0] - outputs[1])), 1e-5)
    self.assertIn(self.device.components[0], outputs[0].backing_device)
    self.assertIn(self.device.components[1], outputs[1].backing_device)

  def test_layer_sync_training(self):
    with ops.device(self.device.name):
      layer = _Dense(5)

      with backprop.GradientTape() as tape:
        x = self.device.pack(
            [constant_op.constant([[-0.5]]),
             constant_op.constant([[0.5]])])
        y = layer(x)
        loss = (y - math_ops.range(5.))**2.
      parameters = layer.trainable_variables
      unreduced_gradients = tape.gradient(loss, parameters)
      reduced_gradients = _collective_sum(unreduced_gradients, num_replicas=2)
      for grad, param in zip(reduced_gradients, parameters):
        param.assign_sub(0.01 * grad)
    final_kernels = self.device.unpack(layer.kernel)
    self.assertAllClose(final_kernels[0], final_kernels[1])
    final_bias = self.device.unpack(layer.bias)
    expected_bias = (1. - 0.01 * 2. * (1. + .5 - math_ops.range(5.)) -
                     0.01 * 2. * (1. - .5 - math_ops.range(5.)))
    self.assertAllClose(expected_bias, final_bias[0])
    self.assertAllClose(expected_bias, final_bias[1])
    self.assertIn(self.device.components[0], final_kernels[0].backing_device)
    self.assertIn(self.device.components[1], final_kernels[1].backing_device)

  def test_layer_divergent_buffer_training(self):
    with ops.device(self.device.name):
      layer = _Dense(5)

      with backprop.GradientTape() as tape:
        x = self.device.pack(
            [constant_op.constant([[-0.5]]),
             constant_op.constant([[0.5]])])
        y = layer(x)
        loss = (y - math_ops.range(5.))**2.
      parameters = layer.trainable_variables
      unreduced_gradients = tape.gradient(loss, parameters)
      for grad, param in zip(unreduced_gradients, parameters):
        param.assign_sub(0.01 * grad)
    final_kernels = self.device.unpack(layer.kernel)
    self.assertNotAllClose(final_kernels[0], final_kernels[1])
    final_bias = self.device.unpack(layer.bias)
    self.assertAllClose(1. - 0.01 * 2. * (1. - .5 - math_ops.range(5.)),
                        final_bias[0])
    self.assertAllClose(1. - 0.01 * 2. * (1. + .5 - math_ops.range(5.)),
                        final_bias[1])
    self.assertIn(self.device.components[0], final_kernels[0].backing_device)
    self.assertIn(self.device.components[1], final_kernels[1].backing_device)

  def test_training_loop(self):
    self.skipTest(
        "Disable saving until SaveableObject's methods are traceable.")
    for _ in range(5):
      layer = _Dense(5)
      checkpoint = tracking.Checkpoint(layer=layer)
      manager = checkpoint_management.CheckpointManager(
          checkpoint, directory=self.get_temp_dir(), max_to_keep=5)
      manager.restore_or_initialize()

      for _ in range(10):
        with self.device.scope():
          with backprop.GradientTape() as tape:
            x = self.device.pack(
                [constant_op.constant([[-0.5]]),
                 constant_op.constant([[0.5]])])
            y = layer(x)
            loss = (y - math_ops.range(5.))**2.
          parameters = layer.trainable_variables
          unreduced_gradients = tape.gradient(loss, parameters)
          reduced_gradients = _collective_sum(
              unreduced_gradients, num_replicas=len(self.device.components))
          for grad, param in zip(reduced_gradients, parameters):
            param.assign_sub(0.01 * grad)

        manager.save()


if __name__ == "__main__":
  ops.enable_eager_execution()
  test.main()
