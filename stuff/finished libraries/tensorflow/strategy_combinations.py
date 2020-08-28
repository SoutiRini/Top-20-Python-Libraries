# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
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
"""Strategy combinations for combinations.combine()."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import atexit

from tensorflow.python import tf2
from tensorflow.python.distribute import central_storage_strategy
from tensorflow.python.distribute import cluster_resolver
from tensorflow.python.distribute import collective_all_reduce_strategy
from tensorflow.python.distribute import combinations
from tensorflow.python.distribute import distribution_strategy_context
from tensorflow.python.distribute import mirrored_strategy as mirrored_lib
from tensorflow.python.distribute import multi_process_runner
from tensorflow.python.distribute import one_device_strategy as one_device_lib
from tensorflow.python.distribute import tpu_strategy as tpu_lib
from tensorflow.python.distribute.cluster_resolver import tpu_cluster_resolver
from tensorflow.python.eager import context
from tensorflow.python.eager import remote
from tensorflow.python.framework import config
from tensorflow.python.platform import flags
from tensorflow.python.tpu import device_assignment as device_assignment_lib
from tensorflow.python.tpu import tpu_strategy_util

FLAGS = flags.FLAGS

_did_connect_to_cluster = False


# pylint: disable=missing-docstring
def _get_tpu_strategy_creator(steps_per_run,
                              use_single_core=False,
                              enable_packed_variable=False,
                              **kwargs):

  def _create_tpu_strategy():
    global _did_connect_to_cluster

    try:
      # Attempt to locally discover the TPU. This will fail for Cloud TPU, in
      # which case we fall back to the values passed as flags.
      resolver = tpu_cluster_resolver.TPUClusterResolver()
      did_automatically_resolve = True
    except ValueError:
      did_automatically_resolve = False

      # These flags will be defined by tpu_test_wrapper.py.
      resolver = tpu_cluster_resolver.TPUClusterResolver(
          tpu=hasattr(FLAGS, "tpu") and FLAGS.tpu or "",
          zone=hasattr(FLAGS, "zone") and FLAGS.zone or None,
          project=hasattr(FLAGS, "project") and FLAGS.project or None,
      )

    # Only connect once per process, rather than per test method.
    if getattr(FLAGS, "tpu", "") or did_automatically_resolve:
      if not _did_connect_to_cluster:
        remote.connect_to_cluster(resolver)
        _did_connect_to_cluster = True

    topology = tpu_strategy_util.initialize_tpu_system(resolver)
    device_assignment = None
    if use_single_core:
      device_assignment = device_assignment_lib.DeviceAssignment(
          topology, core_assignment=device_assignment_lib.
          SINGLE_CORE_ASSIGNMENT)

    # Steps per run is only supported in TF 1.x
    if tf2.enabled():
      strategy = tpu_lib.TPUStrategy(resolver, device_assignment, **kwargs)
    else:
      strategy = tpu_lib.TPUStrategyV1(resolver, steps_per_run,
                                       device_assignment, **kwargs)
    strategy._enable_packed_variable_in_eager_mode = enable_packed_variable  # pylint: disable=protected-access
    return strategy

  return _create_tpu_strategy


def _get_multi_worker_mirrored_creator(required_gpus):

  def _create_multi_worker_mirrored():
    tf_config = cluster_resolver.TFConfigClusterResolver()
    master = tf_config.master()
    if tf_config.rpc_layer:
      # Strip off the rpc_layer suffix.
      master = master[len("%s://" % tf_config.rpc_layer):]
    resolver = cluster_resolver.SimpleClusterResolver(
        cluster_spec=tf_config.cluster_spec(),
        task_type=tf_config.task_type,
        task_id=tf_config.task_id,
        master=master,
        environment=tf_config.environment,
        num_accelerators={"GPU": required_gpus},
        rpc_layer=tf_config.rpc_layer or "grpc",
    )
    # Always create the strategy in eager mode so that it starts the server and
    # configures the eager context. The eager context can no longer be
    # configured after initialization.
    with context.eager_mode():
      strategy = collective_all_reduce_strategy.CollectiveAllReduceStrategy(
          cluster_resolver=resolver)
    # TODO(b/152320929): Wait for the cluster before proceeding, otherwise
    # collectives may hang if any worker launches collectives before the chief
    # creates the strategy.
    try:
      multi_process_runner.barrier().wait()
    except ValueError:
      # If the creator is called in the main process,
      # multi_process_runner.barrier() raises ValueError, which is safe to
      # ignore.
      pass
    return strategy

  return _create_multi_worker_mirrored


# pylint: disable=g-long-lambda
default_strategy = combinations.NamedDistribution(
    "Default",
    distribution_strategy_context._get_default_strategy,  # pylint: disable=protected-access
    required_gpus=None)
one_device_strategy = combinations.NamedDistribution(
    "OneDeviceCPU",
    lambda: one_device_lib.OneDeviceStrategy("/cpu:0"),
    required_gpus=None)
one_device_strategy_gpu = combinations.NamedDistribution(
    "OneDeviceGPU",
    lambda: one_device_lib.OneDeviceStrategy("/gpu:0"),
    required_gpus=1)
one_device_strategy_on_worker_1 = combinations.NamedDistribution(
    "OneDeviceOnWorker1CPU",
    lambda: one_device_lib.OneDeviceStrategy("/job:worker/replica:0/task:1/cpu:0"),  # pylint: disable=line-too-long
    required_gpus=None)
one_device_strategy_gpu_on_worker_1 = combinations.NamedDistribution(
    "OneDeviceOnWorker1GPU",
    lambda: one_device_lib.OneDeviceStrategy("/job:worker/replica:0/task:1/gpu:0"),  # pylint: disable=line-too-long
    required_gpus=1)
tpu_strategy = combinations.NamedDistribution(
    "TPU", _get_tpu_strategy_creator(steps_per_run=2), required_tpu=True)
tpu_strategy_packed_var = combinations.NamedDistribution(
    "TPUPackedVar",
    _get_tpu_strategy_creator(steps_per_run=2, enable_packed_variable=True),
    required_tpu=True)
tpu_strategy_one_step = combinations.NamedDistribution(
    "TPUOneStep", _get_tpu_strategy_creator(steps_per_run=1), required_tpu=True)
tpu_strategy_one_core = combinations.NamedDistribution(
    "TPUOneCore",
    _get_tpu_strategy_creator(steps_per_run=2, use_single_core=True),
    required_tpu=True)
tpu_strategy_one_step_one_core = combinations.NamedDistribution(
    "TPUOneStepOneCore",
    _get_tpu_strategy_creator(steps_per_run=1, use_single_core=True),
    required_tpu=True)
cloud_tpu_strategy = combinations.NamedDistribution(
    "CloudTPU",
    _get_tpu_strategy_creator(steps_per_run=2),
    required_tpu=True,
    use_cloud_tpu=True)
mirrored_strategy_with_one_cpu = combinations.NamedDistribution(
    "Mirrored1CPU", lambda: mirrored_lib.MirroredStrategy(["/cpu:0"]))
mirrored_strategy_with_one_gpu = combinations.NamedDistribution(
    "Mirrored1GPU",
    lambda: mirrored_lib.MirroredStrategy(["/gpu:0"]),
    required_gpus=1)
mirrored_strategy_with_gpu_and_cpu = combinations.NamedDistribution(
    "MirroredCPUAndGPU",
    lambda: mirrored_lib.MirroredStrategy(["/gpu:0", "/cpu:0"]),
    required_gpus=1)
mirrored_strategy_with_two_gpus = combinations.NamedDistribution(
    "Mirrored2GPUs",
    lambda: mirrored_lib.MirroredStrategy(["/gpu:0", "/gpu:1"]),
    required_gpus=2)
# Should call set_virtual_cpus_to_at_least(3) in your test's setUp methods.
mirrored_strategy_with_cpu_1_and_2 = combinations.NamedDistribution(
    "Mirrored2CPU", lambda: mirrored_lib.MirroredStrategy(["/cpu:1", "/cpu:2"]))
central_storage_strategy_with_two_gpus = combinations.NamedDistribution(
    "CentralStorage2GPUs",
    lambda: central_storage_strategy.CentralStorageStrategy._from_num_gpus(2),  # pylint: disable=protected-access
    required_gpus=2)
central_storage_strategy_with_gpu_and_cpu = combinations.NamedDistribution(
    "CentralStorageCPUAndGPU",
    lambda: central_storage_strategy.CentralStorageStrategy(
        ["/gpu:0", "/cpu:0"]),
    required_gpus=1)
# chief + 1 worker, with CPU.
multi_worker_mirrored_2x1_cpu = combinations.NamedDistribution(
    "MultiWorkerMirrored2x1CPU",
    _get_multi_worker_mirrored_creator(required_gpus=0),
    has_chief=True,
    num_workers=1,
)
# chief + 1 worker, with 1 GPU each.
multi_worker_mirrored_2x1_gpu = combinations.NamedDistribution(
    "MultiWorkerMirrored2x1GPU",
    _get_multi_worker_mirrored_creator(required_gpus=1),
    has_chief=True,
    num_workers=1,
    required_gpus=1,
)
# chief + 1 worker, with 2 GPU each.
multi_worker_mirrored_2x2_gpu = combinations.NamedDistribution(
    "MultiWorkerMirrored2x2GPU",
    _get_multi_worker_mirrored_creator(required_gpus=2),
    has_chief=True,
    num_workers=1,
    required_gpus=2,
)
# chief + 3 workers, with CPU.
multi_worker_mirrored_4x1_cpu = combinations.NamedDistribution(
    "MultiWorkerMirrored4x1CPU",
    _get_multi_worker_mirrored_creator(required_gpus=0),
    has_chief=True,
    num_workers=3,
)


# Shutdown the runners gracefully to avoid the processes getting SIGTERM.
def _shutdown_at_exit():
  for strategy in [
      multi_worker_mirrored_2x1_cpu,
      multi_worker_mirrored_2x1_gpu,
      multi_worker_mirrored_2x2_gpu,
      multi_worker_mirrored_4x1_cpu,
  ]:
    if strategy.runner:
      strategy.runner.shutdown()


atexit.register(_shutdown_at_exit)


graph_and_eager_modes = ["graph", "eager"]


# This function should be called in a test's `setUp` method with the
# maximum value needed in any test.
def set_virtual_cpus_to_at_least(num_virtual_cpus):
  """Create virtual CPU devices if they haven't yet been created."""
  if num_virtual_cpus < 1:
    raise ValueError("`num_virtual_cpus` must be at least 1 not %r" %
                     (num_virtual_cpus,))
  physical_devices = config.list_physical_devices("CPU")
  if not physical_devices:
    raise RuntimeError("No CPUs found")
  configs = config.get_logical_device_configuration(physical_devices[0])
  if configs is None:
    logical_devices = [
        context.LogicalDeviceConfiguration() for _ in range(num_virtual_cpus)
    ]
    config.set_logical_device_configuration(physical_devices[0],
                                            logical_devices)
  else:
    if len(configs) < num_virtual_cpus:
      raise RuntimeError("Already configured with %d < %d virtual CPUs" %
                         (len(configs), num_virtual_cpus))


strategies_minus_tpu = [
    default_strategy,
    one_device_strategy,
    one_device_strategy_gpu,
    mirrored_strategy_with_gpu_and_cpu,
    mirrored_strategy_with_two_gpus,
    central_storage_strategy_with_gpu_and_cpu,
]

strategies_minus_default_and_tpu = [
    one_device_strategy,
    one_device_strategy_gpu,
    mirrored_strategy_with_gpu_and_cpu,
    mirrored_strategy_with_two_gpus,
]

tpu_strategies = [
    tpu_strategy,  # steps_per_run=2
    tpu_strategy_one_step,
    tpu_strategy_packed_var,
    cloud_tpu_strategy,
]

all_strategies_minus_default = strategies_minus_default_and_tpu + tpu_strategies

all_strategies = strategies_minus_tpu + tpu_strategies

two_replica_strategies = [
    mirrored_strategy_with_gpu_and_cpu,
    mirrored_strategy_with_two_gpus,
    multi_worker_mirrored_2x1_cpu,
    multi_worker_mirrored_2x1_gpu,
    tpu_strategy,  # steps_per_run=2
    tpu_strategy_one_step,
    central_storage_strategy_with_gpu_and_cpu,
]

four_replica_strategies = [
    multi_worker_mirrored_2x2_gpu,
    multi_worker_mirrored_4x1_cpu,
]

# TODO(b/159831907): replace with two_replica_strategies after the tests using
# it work with MWMS.
multidevice_strategies = [
    mirrored_strategy_with_gpu_and_cpu,
    mirrored_strategy_with_two_gpus,
    tpu_strategy,  # steps_per_run=2
    tpu_strategy_one_step
]

multiworker_strategies = [
    multi_worker_mirrored_2x1_cpu,
    multi_worker_mirrored_2x1_gpu,
    multi_worker_mirrored_2x2_gpu
]


def strategy_minus_tpu_combinations():
  return combinations.combine(
      distribution=strategies_minus_tpu, mode=["graph", "eager"])


def tpu_strategy_combinations():
  return combinations.combine(distribution=tpu_strategies, mode=["graph"])


def all_strategy_combinations():
  return strategy_minus_tpu_combinations() + tpu_strategy_combinations()


def all_strategy_minus_default_and_tpu_combinations():
  return combinations.combine(
      distribution=[
          one_device_strategy, one_device_strategy_gpu,
          mirrored_strategy_with_gpu_and_cpu, mirrored_strategy_with_two_gpus
      ],
      mode=["graph", "eager"])


def all_strategy_combinations_minus_default():
  return (all_strategy_minus_default_and_tpu_combinations() +
          tpu_strategy_combinations())
