from __future__ import absolute_import, division, print_function, unicode_literals
import copy
import errno
import fcntl
import os
import sys
import time
import tempfile
import unittest
from contextlib import contextmanager
from datetime import timedelta
from functools import reduce, wraps
from io import StringIO

import torch
import torch.cuda
import torch.distributed as dist
import torch.nn as nn
import torch.nn.functional as F
from torch.testing._internal.common_utils import TestCase, run_tests, find_free_port
from torch.nn.parallel.distributed import _dump_DDP_relevant_env_vars
from torch.distributed.distributed_c10d import _get_default_group
from torch._utils_internal import TEST_MASTER_ADDR as MASTER_ADDR
from torch._utils_internal import TEST_MASTER_PORT as MASTER_PORT
from torch.testing._internal.common_distributed import (
    TEST_SKIPS,
    MultiProcessTestCase,
    simple_sparse_reduce_tests,
    skip_if_rocm,
    skip_if_small_worldsize,
    skip_if_lt_x_gpu,
    skip_if_no_gpu,
)

try:
    import torchvision
    HAS_TORCHVISION = True
except ImportError:
    HAS_TORCHVISION = False


skipIfNoTorchVision = unittest.skipIf(not HAS_TORCHVISION, "no torchvision")

CPP_EXTENSIONS_WARNING = """
Ninja (https://ninja-build.org) must be available to run C++ extensions tests,
but it could not be found. Install ninja with `pip install ninja`
or `conda install ninja`.
"""

BACKEND = os.environ["BACKEND"]
TEMP_DIR = os.environ["TEMP_DIR"]
INIT_METHOD = os.getenv("INIT_METHOD", "env://")

DEFAULT_TIMEOUT = 300
CUSTOMIZED_TIMEOUT = {"test_DistributedDataParallel": 500}


class _FC2(nn.Module):
    def __init__(self):
        super(_FC2, self).__init__()
        self.fc = nn.Linear(10, 50, bias=True)
        self.fc.bias.requires_grad = False

    def forward(self, x):
        x = self.fc(x)
        return x


class Net(nn.Module):
    def __init__(self):
        super(Net, self).__init__()
        self.fc1 = nn.Linear(2, 10, bias=False)
        self.fc2 = _FC2()
        self.fc3 = nn.Linear(50, 4, bias=False)
        self.relu = nn.ReLU()
        self.no_grad_param = nn.Parameter(torch.tensor([2, 2]).long(),
                                          requires_grad=False)

    def forward(self, x):
        x = self.relu(self.fc1(x))
        x = self.relu(self.fc2(x))
        x = self.fc3(x)
        return F.softmax(x, dim=1)


class BatchNormNet(nn.Module):

    def __init__(self):
        super(BatchNormNet, self).__init__()
        self.fc1 = nn.Linear(2, 40, bias=False)
        self.bn = nn.BatchNorm1d(4)
        self.fc2 = nn.Linear(40, 4, bias=False)

    def forward(self, x):
        x = torch.reshape(self.fc1(x), (-1, 4, 10))
        x = self.bn(x)
        x = torch.reshape(x, (-1, 40))
        x = self.fc2(x)
        return F.softmax(x, dim=1)


DDP_NET = Net()
BN_NET = BatchNormNet()
ONLY_SBN_NET = nn.SyncBatchNorm(2, momentum=0.99)


def get_timeout(test_id):
    test_name = test_id.split(".")[-1]
    if test_name in CUSTOMIZED_TIMEOUT:
        return CUSTOMIZED_TIMEOUT[test_name]
    else:
        return DEFAULT_TIMEOUT


if not dist.is_available():
    print("Distributed not available, skipping tests")
    sys.exit(0)


def skip_if_no_ninja(func):

    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            import torch.utils.cpp_extension
            torch.utils.cpp_extension.verify_ninja_availability()
        except RuntimeError:
            print(CPP_EXTENSIONS_WARNING)
            return 0

        return func(*args, **kwargs)

    return wrapper

def require_backend(backends):
    if BACKEND not in backends:
        return unittest.skip("Test requires backend to be one of %s" % backends)
    return lambda func: func


def require_backends_available(backends):
    def check(backend):
        if backend == dist.Backend.GLOO:
            return dist.is_gloo_available()
        if backend == dist.Backend.NCCL:
            return dist.is_nccl_available()
        if backend == dist.Backend.MPI:
            return dist.is_mpi_available()
        return False
    backends = map(lambda b: dist.Backend(b), backends)
    if not all(map(check, backends)):
        return unittest.skip(
            "Test requires backends to be available %s" % backends)
    return lambda func: func


def require_world_size(world_size):
    if int(os.environ["WORLD_SIZE"]) < world_size:
        return unittest.skip("Test requires world size of %d" % world_size)
    return lambda func: func


def apply_hack_for_nccl():
    # This is a hack for a known NCCL issue using multiprocess
    # in conjunction with multiple threads to manage different GPUs which
    # may cause ncclCommInitRank to fail.
    # http://docs.nvidia.com/deeplearning/sdk/nccl-release-notes/rel_2.1.4.html#rel_2.1.4
    # It slows down the performance of collective operations.
    # Without this setting NCCL might throw unhandled error.
    os.environ["NCCL_MAX_NRINGS"] = "1"


@contextmanager
def _lock():
    lockfile = os.path.join(TEMP_DIR, "lockfile")
    with open(lockfile, "w") as lf:
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            lf.close()


def _build_tensor(size, value=None, dtype=torch.float):
    if value is None:
        value = size
    return torch.empty(size, size, size, dtype=dtype).fill_(value)


def _build_multidim_tensor(dim, dim_size, value=None):
    if value is None:
        value = size
    return torch.FloatTensor(size=[dim_size for _ in range(dim)]).fill_(value)


class Barrier(object):
    barrier_id = 0

    @classmethod
    def init(cls):
        cls.barrier_id = 0
        barrier_dir = os.path.join(TEMP_DIR, "barrier")
        for f_name in os.listdir(barrier_dir):
            os.unlink(os.path.join(barrier_dir, f_name))

    @classmethod
    def sync(cls, wait_for=None, timeout=10):
        if wait_for is None:
            wait_for = dist.get_world_size()
        cls.barrier_id += 1
        barrier_dir = os.path.join(TEMP_DIR, "barrier")
        pid = str(os.getpid())
        barrier_file = os.path.join(barrier_dir, pid)
        with _lock():
            with open(barrier_file, "w") as f:
                f.write(str(cls.barrier_id))

        start_time = time.time()
        while True:
            arrived = 0
            with _lock():
                for f_name in os.listdir(barrier_dir):
                    with open(os.path.join(barrier_dir, f_name), "r") as f:
                        data = f.read()
                        if int(data) >= cls.barrier_id:
                            arrived += 1
            if arrived == wait_for:
                break

            if time.time() - start_time > timeout:
                raise RuntimeError("barrier timeout")
            time.sleep(0.1)


@contextmanager
def _captured_output():
    new_out, new_err = StringIO(), StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = new_out, new_err
        yield sys.stdout, sys.stderr
    finally:
        sys.stdout, sys.stderr = old_out, old_err


class _DistTestBase(object):
    def _barrier(self, *args, **kwargs):
        Barrier.sync(*args, **kwargs)

    def _init_group_test(self, **kwargs):
        group = [1, 2]
        group_id = dist.new_group(group, **kwargs)
        rank = dist.get_rank()
        if rank not in group:
            return ([], None, rank)

        return (group, group_id, rank)

    def _init_full_group_test(self, **kwargs):
        group = list(range(0, dist.get_world_size()))
        group_id = dist.new_group(**kwargs)
        rank = dist.get_rank()
        return (group, group_id, rank)

    def _init_global_test(self):
        group = list(range(0, dist.get_world_size()))
        group_id = dist.group.WORLD
        rank = dist.get_rank()
        return (group, group_id, rank)

    # HELPER FOR MULTIGPU TESTS
    def _init_multigpu_helper(self):
        """Multigpu tests are designed to simulate the multi nodes with multi
        GPUs on each node. Nccl backend requires equal #GPUs in each process.
        On a single node, all visible GPUs are evenly
        divided to subsets, each process only uses a subset.
        """
        nGPUs = torch.cuda.device_count()
        world_size = dist.get_world_size()
        visible_devices = range(nGPUs)

        if BACKEND == "nccl":
            apply_hack_for_nccl()

        nGPUs_per_process = nGPUs // world_size
        rank_to_GPU = {
            i: list(
                visible_devices[i * nGPUs_per_process: (i + 1) * nGPUs_per_process]
            )
            for i in range(world_size)
        }
        return rank_to_GPU

    def test_dump_DDP_relevant_env_vars(self):
        with _captured_output() as (out, err):
            _dump_DDP_relevant_env_vars()
            lines = out.getvalue().splitlines()

        def format_line(var):
            return "env:%s=%s" % (var, os.environ[var] if var in os.environ else "N/A")

        # Check relevant env vars
        vars = [
            "MASTER_ADDR",
            "MASTER_PORT",
            "WORLD_SIZE",
            "NCCL_TOPO_DUMP_FILE",  # N/A
        ]
        for var in vars:
            line = format_line(var)
            self.assertIn(line, lines)
        # Check irrelevant env vars
        vars = [
            "xxx",
            "yyy",
            "zzz",
        ]
        for var in vars:
            line = format_line(var)
            self.assertNotIn(line, lines)

    # GET RANK
    def test_get_rank(self):
        test_dir = os.path.join(TEMP_DIR, "test_dir")
        pid = str(os.getpid())
        num_processes = dist.get_world_size()
        with open(os.path.join(test_dir, pid), "w") as f:
            f.write(str(dist.get_rank()))

        self._barrier()

        all_ranks = set()
        for f_name in os.listdir(test_dir):
            with open(os.path.join(test_dir, f_name), "r") as f:
                all_ranks.add(int(f.read()))
        self.assertEqual(len(all_ranks), num_processes)

        self._barrier()

        if dist.get_rank() == 0:
            for f_name in os.listdir(test_dir):
                os.unlink(os.path.join(test_dir, f_name))

        self._barrier()

    def test_get_backend(self):
        if dist.get_world_size() > 2:
            group = [1, 2]
        else:
            group = [0, 1]
        group_id = dist.new_group(group)
        backend_str = BACKEND.lower()
        self.assertEqual(dist.get_backend(), backend_str)
        if dist.get_rank() in group:
            self.assertEqual(dist.get_backend(group_id), backend_str)
        else:
            with self.assertRaisesRegex(RuntimeError, "Invalid process group specified"):
                dist.get_backend(group_id)

    def test_Backend_enum_class(self):
        # test parsing
        backend = BACKEND.lower()
        self.assertEqual(dist.Backend(BACKEND.upper()), backend)
        self.assertEqual(dist.Backend(BACKEND), backend)
        with self.assertRaisesRegex(ValueError, "Invalid backend: 'undefined'"):
            dist.Backend("undefined")
        with self.assertRaisesRegex(ValueError, "Invalid backend: 'xYz'"):
            dist.Backend("xYz")
        with self.assertRaises(ValueError):
            dist.Backend(None)
        with self.assertRaises(ValueError):
            dist.Backend(3)
        with self.assertRaises(ValueError):
            dist.Backend(["gloo"])

    # Test destroy
    def test_destroy_group(self):
        if dist.get_world_size() > 2:
            group = [1, 2]
        else:
            group = [0, 1]
        group_id = dist.new_group(group)
        self._barrier()
        dist.destroy_process_group(group_id)

    # Test get rank and size of group
    def test_get_rank_size_group(self):
        if dist.get_world_size() > 2:
            group = [1, 2]
        else:
            group = [0, 1]
        group_id = dist.new_group(group)
        if dist.get_rank() in group:
            self.assertEqual(dist.get_world_size(group_id), 2)
            self.assertTrue(dist.get_rank(group_id) in list(range(2)))
        else:
            self.assertEqual(dist.get_world_size(group_id), -1)
            self.assertEqual(dist.get_rank(group_id), -1)

    # Test destroy full groups
    def test_destroy_full_group(self):
        _, group_id, _ = self._init_full_group_test()
        self._barrier()
        dist.destroy_process_group(group_id)

    # Test get rank and size of full group
    def test_get_rank_size_full_group(self):
        _, group_id, _ = self._init_full_group_test()
        self.assertEqual(dist.get_world_size(group_id), dist.get_world_size())
        self.assertEqual(dist.get_rank(group_id), dist.get_rank())

    def _test_barrier_timeout(self, group_id, timeout):
        local_rank = dist.get_rank(group_id)

        # Only execute barrier on rank == 0, causing it to timeout
        if local_rank == 0:
            expected_time = time.time() + timeout.total_seconds()
            with self.assertRaisesRegex(Exception, " (Timed out|closed|timeout) "):
                dist.barrier(group_id)
            self.assertGreaterEqual(time.time(), expected_time)
        else:
            time.sleep(timeout.total_seconds())

    @unittest.skipIf(BACKEND != "gloo", "Only gloo backend supports timeouts")
    @unittest.skipIf(
        not INIT_METHOD.startswith("file://"),
        "Requires file:// initialization method. " +
        "Both tcp:// and env:// rely on the TCP store for which "
        "reinitialization has proven racy."
    )
    def test_barrier_timeout_global(self):
        dist.destroy_process_group()

        # Explicitly pass world size to the barrier because we've
        # just destroyed any state in torch.distributed.
        self._barrier(wait_for=int(WORLD_SIZE))

        # Reinitialize global process group
        timeout = timedelta(seconds=1)
        dist.init_process_group(
            init_method=INIT_METHOD,
            backend=BACKEND,
            world_size=int(WORLD_SIZE),
            rank=self.rank,
            timeout=timeout,
        )
        self._test_barrier_timeout(dist.group.WORLD, timeout)

    @skip_if_small_worldsize
    @unittest.skipIf(BACKEND != "gloo", "Only gloo backend supports timeouts")
    def test_barrier_timeout_group(self):
        timeout = timedelta(seconds=1)
        _, group_id, _ = self._init_group_test(timeout=timeout)
        if group_id is not None:
            self._test_barrier_timeout(group_id, timeout)

    @unittest.skipIf(BACKEND != "gloo", "Only gloo backend supports timeouts")
    def test_barrier_timeout_full_group(self):
        timeout = timedelta(seconds=1)
        _, group_id, _ = self._init_full_group_test(timeout=timeout)
        if group_id is not None:
            self._test_barrier_timeout(group_id, timeout)

    # This test helper can only be used when using the Gloo or NCCL backend
    # **and** both the Gloo and NCCL backends are available.
    # See the @skip annotations below.
    def _test_group_override_backend(self, initializer):
        if BACKEND == "gloo":
            new_backend = "nccl"
        if BACKEND == "nccl":
            new_backend = "gloo"

        group, group_id, rank = initializer(backend=new_backend)
        if group_id is None:
            return

        if new_backend == "gloo":
            self.assertTrue(isinstance(group_id, dist.ProcessGroupGloo))
        if new_backend == "nccl":
            self.assertTrue(isinstance(group_id, dist.ProcessGroupNCCL))

        self.assertEqual(rank, group[dist.get_rank(group_id)])
        self.assertEqual(len(group), dist.get_world_size(group_id))

        # Pin device (so we avoid NCCL race conditions/deadlocks).
        group_rank = dist.get_rank(group_id)
        torch.cuda.set_device(group_rank)

        # Run broadcast of CUDA tensor (so it works for both Gloo and NCCL).
        tensor = _build_tensor(2, value=group_rank).cuda()
        dist.broadcast(tensor, src=group[0], group=group_id)
        self.assertEqual(_build_tensor(2, value=0), tensor.to("cpu"))

    @require_backend({"gloo", "nccl"})
    @require_backends_available({"gloo", "nccl"})
    @require_world_size(3)
    @skip_if_lt_x_gpu(2)
    def test_backend_group(self):
        self._test_group_override_backend(self._init_group_test)

    @require_backend({"gloo", "nccl"})
    @require_backends_available({"gloo", "nccl"})
    @skip_if_lt_x_gpu(3)
    def test_backend_full_group(self):
        self._test_group_override_backend(self._init_full_group_test)

    # SEND RECV
    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support send/recv")
    def test_send_recv(self):
        rank = dist.get_rank()
        tensor = _build_tensor(rank + 1)

        for src in range(0, dist.get_world_size()):
            if src == rank:
                # Send mode
                for dst in range(0, dist.get_world_size()):
                    if dst == rank:
                        continue
                    dist.send(tensor, dst)
            else:
                # Recv mode
                expected_tensor = _build_tensor(src + 1)
                output_tensor = _build_tensor(src + 1, value=-1)
                dist.recv(output_tensor, src)
                self.assertEqual(output_tensor, expected_tensor)

        self._barrier()

    # SEND RECV ANY SOURCE
    @unittest.skipIf(
        BACKEND == "nccl", "Nccl does not support send/recv from any source"
    )
    def test_send_recv_any_source(self):
        rank = dist.get_rank()
        tensor = _build_tensor(10, value=rank)
        recv_ranks = set()

        for dst in range(0, dist.get_world_size()):
            if dst == rank:
                # Recv mode
                for dst in range(0, dist.get_world_size()):
                    if dst == rank:
                        continue
                    output_tensor = _build_tensor(10, value=-1)
                    sender = dist.recv(output_tensor)

                    # Assert the scalar value "sender" that should be
                    # equal to the rank of the sender is equal to all
                    # values in the received tensor.
                    self.assertTrue(output_tensor.eq(sender).all())
                    recv_ranks.add(sender)
            else:
                # Send mode
                dist.send(tensor, dst)

        self.assertEqual(len(recv_ranks), dist.get_world_size() - 1)
        self._barrier()

    # SEND RECV WITH TAG
    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support send/recv")
    def test_send_recv_with_tag(self):
        rank = dist.get_rank()
        world_size = dist.get_world_size()
        tensor = _build_tensor(10, value=rank)

        for dst in range(0, world_size):
            if dst == rank:
                # Recv mode
                for src in range(0, world_size):
                    if src == rank:
                        continue
                    output_tensor = _build_tensor(10, value=-1)
                    dist.recv(output_tensor, src, tag=src)
                    self.assertTrue(output_tensor.eq(src).all())
            else:
                # Send mode
                dist.send(tensor, dst, tag=rank)

    # ISEND
    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support isend")
    def test_isend(self):
        rank = dist.get_rank()
        world_size = dist.get_world_size()

        if rank == 0:
            requests = [
                dist.isend(_build_tensor(dest, 10), dest)
                for dest in range(1, world_size)
            ]
            for request in requests:
                request.wait()
                self.assertTrue(request.is_completed())
        else:
            tensor = _build_tensor(rank, -1)
            dist.recv(tensor, 0)
            self.assertEqual(tensor, _build_tensor(rank, 10))

        self._barrier()

    # IRECV
    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support irecv")
    def test_irecv(self):
        rank = dist.get_rank()
        world_size = dist.get_world_size()

        if rank == 0:
            expected_tensors = [_build_tensor(src, -1) for src in range(1, world_size)]
            requests = [
                dist.irecv(expected_tensors[src - 1], src)
                for src in range(1, world_size)
            ]

            for src in range(1, world_size):
                requests[src - 1].wait()
                self.assertTrue(requests[src - 1].is_completed())
                self.assertEqual(expected_tensors[src - 1], _build_tensor(src, 10))
        else:
            tensor = _build_tensor(rank, 10)
            dist.send(tensor, 0)

        self._barrier()

    # BROADCAST
    def _test_broadcast_helper(
        self, group, group_id, rank, cuda=False, rank_to_GPU=None
    ):
        for dtype, value, requires_cuda in [
            (torch.float, -1e-10, False),
            (torch.double, -1e-100, False),
            (torch.half, -0.1, True),
            (torch.int8, -2, False),
            (torch.uint8, 129, False),
            (torch.int, -1e5, False),
            (torch.long, -1e15, False),
        ]:
            if requires_cuda and not cuda:
                continue
            for src in group:
                expected_tensor = _build_tensor(src + 1, value, dtype)
                if cuda:
                    expected_tensor = expected_tensor.cuda(rank_to_GPU[rank][0])
                if rank == src:
                    dist.broadcast(expected_tensor, src, group_id)
                else:
                    tensor = _build_tensor(src + 1, -1, dtype)
                    if cuda:
                        tensor = tensor.cuda(rank_to_GPU[rank][0])
                    dist.broadcast(tensor, src, group_id)
                    self.assertEqual(tensor.size(), expected_tensor.size())
                    self.assertEqual(tensor.ne(expected_tensor).max(), torch.tensor(False))

        self._barrier()

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_broadcast(self):
        group, group_id, rank = self._init_global_test()
        self._test_broadcast_helper(group, group_id, rank)

    @unittest.skipIf(
        BACKEND != "gloo" and BACKEND != "nccl",
        "Only Gloo and Nccl backend supports CUDA allReduce",
    )
    @skip_if_no_gpu
    def test_broadcast_cuda(self):
        group, group_id, rank = self._init_global_test()
        rank_to_GPU = self._init_multigpu_helper()
        self._test_broadcast_helper(group, group_id, rank, True, rank_to_GPU)

    @skip_if_small_worldsize
    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_broadcast_group(self):
        group, group_id, rank = self._init_group_test()
        self._test_broadcast_helper(group, group_id, rank)

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_broadcast_full_group(self):
        group, group_id, rank = self._init_full_group_test()
        self._test_broadcast_helper(group, group_id, rank)

    # REDUCE
    def _test_reduce_helper(
        self,
        group,
        group_id,
        rank,
        op,
        master_value,
        worker_value,
        expected_value,
        cuda=False,
        rank_to_GPU=None,
    ):
        for src in group:
            if rank == src:
                tensor = _build_tensor(src + 1).fill_(master_value)
                if cuda:
                    tensor = tensor.cuda(rank_to_GPU[rank][0])
                dist.reduce(tensor, src, op, group_id)
                self.assertEqual(tensor, _build_tensor(src + 1, expected_value))
            else:
                tensor = _build_tensor(src + 1).fill_(worker_value)
                if cuda:
                    tensor = tensor.cuda(rank_to_GPU[rank][0])
                dist.reduce(tensor, src, op, group_id)

        self._barrier()

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_reduce_sum(self):
        group, group_id, rank = self._init_global_test()
        self._test_reduce_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.SUM,
            2,
            10,
            2 + (10 * (len(group) - 1)),
        )

    @unittest.skipIf(BACKEND != "nccl", "Only Nccl supports CUDA reduce")
    @skip_if_no_gpu
    @skip_if_rocm
    def test_reduce_sum_cuda(self):
        group, group_id, rank = self._init_global_test()
        rank_to_GPU = self._init_multigpu_helper()
        self._test_reduce_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.SUM,
            2,
            10,
            2 + 10 * (len(group) - 1),
            True,
            rank_to_GPU,
        )

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_reduce_product(self):
        group, group_id, rank = self._init_global_test()
        self._test_reduce_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.PRODUCT,
            2,
            10,
            reduce((lambda x, y: x * y), [10] * (len(group) - 1), 2),
        )

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_reduce_min(self):
        group, group_id, rank = self._init_global_test()
        self._test_reduce_helper(group, group_id, rank, dist.ReduceOp.MIN, 1010, 1, 1)

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_reduce_max(self):
        group, group_id, rank = self._init_global_test()
        self._test_reduce_helper(group, group_id, rank, dist.ReduceOp.MAX, -1, 10, 10)

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    @skip_if_small_worldsize
    def test_reduce_group_sum(self):
        group, group_id, rank = self._init_group_test()
        self._test_reduce_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.SUM,
            2,
            10,
            2 + (10 * (len(group) - 1)),
        )

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    @skip_if_small_worldsize
    def test_reduce_group_product(self):
        group, group_id, rank = self._init_group_test()
        self._test_reduce_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.PRODUCT,
            2,
            10,
            reduce((lambda x, y: x * y), [10] * (len(group) - 1), 2),
        )

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    @skip_if_small_worldsize
    def test_reduce_group_min(self):
        group, group_id, rank = self._init_group_test()
        self._test_reduce_helper(group, group_id, rank, dist.ReduceOp.MIN, 1010, 1, 1)

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    @skip_if_small_worldsize
    def test_reduce_group_max(self):
        group, group_id, rank = self._init_group_test()
        self._test_reduce_helper(group, group_id, rank, dist.ReduceOp.MAX, -1, 10, 10)

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_reduce_full_group_sum(self):
        group, group_id, rank = self._init_full_group_test()
        self._test_reduce_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.SUM,
            2,
            10,
            2 + (10 * (len(group) - 1)),
        )

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_reduce_full_group_product(self):
        group, group_id, rank = self._init_full_group_test()
        self._test_reduce_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.PRODUCT,
            2,
            10,
            reduce((lambda x, y: x * y), [10] * (len(group) - 1), 2),
        )

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_reduce_full_group_min(self):
        group, group_id, rank = self._init_full_group_test()
        self._test_reduce_helper(group, group_id, rank, dist.ReduceOp.MIN, 1010, 1, 1)

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_reduce_full_group_max(self):
        group, group_id, rank = self._init_full_group_test()
        self._test_reduce_helper(group, group_id, rank, dist.ReduceOp.MAX, -1, 10, 10)

    # ALL REDUCE
    def _test_all_reduce_helper(
        self,
        group,
        group_id,
        rank,
        op,
        master_value,
        worker_value,
        expected_value,
        cuda=False,
        rank_to_GPU=None,
    ):
        for src in group:
            if rank == src:
                tensor = _build_tensor(src + 1).fill_(master_value)
                if cuda:
                    tensor = tensor.cuda(rank_to_GPU[rank][0])
                dist.all_reduce(tensor, op, group_id)
                self.assertEqual(tensor, _build_tensor(src + 1, expected_value))
            else:
                tensor = _build_tensor(src + 1).fill_(worker_value)
                if cuda:
                    tensor = tensor.cuda(rank_to_GPU[rank][0])
                dist.all_reduce(tensor, op, group_id)
                self.assertEqual(tensor, _build_tensor(src + 1, expected_value))

        self._barrier()

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_all_reduce_sum(self):
        group, group_id, rank = self._init_global_test()
        self._test_all_reduce_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.SUM,
            2,
            10,
            2 + (10 * (len(group) - 1)),
        )

    @unittest.skipIf(
        BACKEND != "gloo",
        "Only Gloo backend will have CUDA allReduce tested",
    )
    @skip_if_no_gpu
    def test_all_reduce_sum_cuda(self):
        group, group_id, rank = self._init_global_test()
        rank_to_GPU = self._init_multigpu_helper()
        self._test_all_reduce_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.SUM,
            2,
            10,
            2 + (10 * (len(group) - 1)),
            True,
            rank_to_GPU,
        )

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_all_reduce_product(self):
        group, group_id, rank = self._init_global_test()
        self._test_all_reduce_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.PRODUCT,
            2,
            10,
            reduce((lambda x, y: x * y), [10] * (len(group) - 1), 2),
        )

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_all_reduce_min(self):
        group, group_id, rank = self._init_global_test()
        self._test_all_reduce_helper(
            group, group_id, rank, dist.ReduceOp.MIN, 1010, 1, 1
        )

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_all_reduce_max(self):
        group, group_id, rank = self._init_global_test()
        self._test_all_reduce_helper(
            group, group_id, rank, dist.ReduceOp.MAX, -1, 10, 10
        )

    @skip_if_small_worldsize
    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_all_reduce_group_sum(self):
        group, group_id, rank = self._init_group_test()
        self._test_all_reduce_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.SUM,
            2,
            10,
            2 + (10 * (len(group) - 1)),
        )

    @skip_if_small_worldsize
    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_all_reduce_group_product(self):
        group, group_id, rank = self._init_group_test()
        self._test_all_reduce_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.PRODUCT,
            2,
            10,
            reduce((lambda x, y: x * y), [10] * (len(group) - 1), 2),
        )

    @skip_if_small_worldsize
    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_all_reduce_group_min(self):
        group, group_id, rank = self._init_group_test()
        self._test_all_reduce_helper(
            group, group_id, rank, dist.ReduceOp.MIN, 1010, 1, 1
        )

    @skip_if_small_worldsize
    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_all_reduce_group_max(self):
        group, group_id, rank = self._init_group_test()
        self._test_all_reduce_helper(
            group, group_id, rank, dist.ReduceOp.MAX, -1, 10, 10
        )

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_all_reduce_full_group_sum(self):
        group, group_id, rank = self._init_full_group_test()
        self._test_all_reduce_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.SUM,
            2,
            10,
            2 + (10 * (len(group) - 1)),
        )

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_all_reduce_full_group_product(self):
        group, group_id, rank = self._init_full_group_test()
        self._test_all_reduce_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.PRODUCT,
            2,
            10,
            reduce((lambda x, y: x * y), [10] * (len(group) - 1), 2),
        )

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_all_reduce_full_group_min(self):
        group, group_id, rank = self._init_full_group_test()
        self._test_all_reduce_helper(
            group, group_id, rank, dist.ReduceOp.MIN, 1010, 1, 1
        )

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_all_reduce_full_group_max(self):
        group, group_id, rank = self._init_full_group_test()
        self._test_all_reduce_helper(
            group, group_id, rank, dist.ReduceOp.MAX, -1, 10, 10
        )

    # SPARSE ALL REDUCE
    def _test_sparse_all_reduce_sum(self, fn):
        group, group_id, rank = self._init_global_test()

        tests = simple_sparse_reduce_tests(
            rank,
            dist.get_world_size(),
            num_inputs=1)
        for (inputs, outputs) in tests:
            tensors = [fn(input) for input in inputs]
            dist.all_reduce(tensors[0], dist.ReduceOp.SUM, group_id)
            self.assertEqual(tensors[0], outputs[0])

    @unittest.skipIf(BACKEND != "gloo", "Only Gloo backend support sparse all reduce")
    def test_sparse_all_reduce_sum(self):
        self._test_sparse_all_reduce_sum(lambda t: t)

    @unittest.skipIf(BACKEND != "gloo", "Only Gloo backend support sparse all reduce")
    @skip_if_no_gpu
    @skip_if_rocm
    def test_sparse_all_reduce_sum_cuda(self):
        self._test_sparse_all_reduce_sum(lambda t: t.clone().cuda())

    # ALL REDUCE - COALESCED
    @staticmethod
    def _all_reduce_coalesced_sum_test_cases(group_size):
        return (
            [2, 3],
            [10, 11],
            [2 + 10 * (group_size - 1), 3 + 11 * (group_size - 1)]
        )

    @staticmethod
    def _all_reduce_coalesced_product_test_cases(group_size):
        return (
            [1, 2],
            [3, 4],
            [1 * 3 ** (group_size - 1), 2 * 4 ** (group_size - 1)]
        )

    @staticmethod
    def _all_reduce_coalesced_min_test_cases(group_size):
        return (
            [1, 4],
            [2, 3],
            [1, 3]
        )

    @staticmethod
    def _all_reduce_coalesced_max_test_cases(group_size):
        return (
            [1, 4],
            [2, 3],
            [2, 4]
        )

    def _test_all_reduce_coalesced_helper(
        self,
        group,
        group_id,
        rank,
        op,
        cuda=False,
        rank_to_GPU=None,
    ):
        test_case_func = {
            dist.ReduceOp.SUM: self._all_reduce_coalesced_sum_test_cases,
            dist.ReduceOp.PRODUCT: self._all_reduce_coalesced_product_test_cases,
            dist.ReduceOp.MIN: self._all_reduce_coalesced_min_test_cases,
            dist.ReduceOp.MAX: self._all_reduce_coalesced_max_test_cases
        }[op]

        master_values, worker_values, expected_values = test_case_func(len(group))

        for src in group:
            tensors = [
                _build_tensor(src + 1, val)
                for val in (master_values if rank == src else worker_values)
            ]
            if cuda:
                tensors = list(map(tensors, lambda t: t.cuda(rank_to_GPU[rank][0])))
            dist.all_reduce_coalesced(tensors, op, group_id)
            self.assertEqual(
                tensors,
                [
                    _build_tensor(src + 1, expected_value)
                    for expected_value in expected_values
                ]
            )

        self._barrier()

    @require_backend({"gloo"})
    def test_all_reduce_coalesced_sum(self):
        group, group_id, rank = self._init_global_test()
        self._test_all_reduce_coalesced_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.SUM,
            cuda=False,
            rank_to_GPU=None,
        )

    @require_backend({"gloo"})
    def test_all_reduce_coalesced_product(self):
        group, group_id, rank = self._init_global_test()
        self._test_all_reduce_coalesced_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.PRODUCT,
            cuda=False,
            rank_to_GPU=None,
        )

    @require_backend({"gloo"})
    def test_all_reduce_coalesced_min(self):
        group, group_id, rank = self._init_global_test()
        self._test_all_reduce_coalesced_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.MIN,
            cuda=False,
            rank_to_GPU=None,
        )

    @require_backend({"gloo"})
    def test_all_reduce_coalesced_max(self):
        group, group_id, rank = self._init_global_test()
        self._test_all_reduce_coalesced_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.MAX,
            cuda=False,
            rank_to_GPU=None
        )

    @skip_if_small_worldsize
    @require_backend({"gloo"})
    def test_all_reduce_coalesced_group_sum(self):
        group, group_id, rank = self._init_group_test()
        self._test_all_reduce_coalesced_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.SUM,
            cuda=False,
            rank_to_GPU=None
        )

    @skip_if_small_worldsize
    @require_backend({"gloo"})
    def test_all_reduce_coalesced_group_product(self):
        group, group_id, rank = self._init_group_test()
        self._test_all_reduce_coalesced_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.PRODUCT,
            cuda=False,
            rank_to_GPU=None
        )

    @skip_if_small_worldsize
    @require_backend({"gloo"})
    def test_all_reduce_coalesced_group_min(self):
        group, group_id, rank = self._init_group_test()
        self._test_all_reduce_coalesced_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.MIN,
            cuda=False,
            rank_to_GPU=None
        )

    @skip_if_small_worldsize
    @require_backend({"gloo"})
    def test_all_reduce_coalesced_group_max(self):
        group, group_id, rank = self._init_group_test()
        self._test_all_reduce_coalesced_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.MAX,
            cuda=False,
            rank_to_GPU=None
        )

    @require_backend({"gloo"})
    def test_all_reduce_coalesced_full_group_sum(self):
        group, group_id, rank = self._init_full_group_test()
        self._test_all_reduce_coalesced_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.SUM,
            cuda=False,
            rank_to_GPU=None
        )

    @require_backend({"gloo"})
    def test_all_reduce_coalesced_full_group_product(self):
        group, group_id, rank = self._init_full_group_test()
        self._test_all_reduce_coalesced_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.PRODUCT,
            cuda=False,
            rank_to_GPU=None
        )

    @require_backend({"gloo"})
    def test_all_reduce_coalesced_full_group_min(self):
        group, group_id, rank = self._init_full_group_test()
        self._test_all_reduce_coalesced_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.MIN,
            cuda=False,
            rank_to_GPU=None,
        )

    @require_backend({"gloo"})
    def test_all_reduce_coalesced_full_group_max(self):
        group, group_id, rank = self._init_full_group_test()
        self._test_all_reduce_coalesced_helper(
            group,
            group_id,
            rank,
            dist.ReduceOp.MAX,
            cuda=False,
            rank_to_GPU=None
        )

    # SCATTER
    def _test_scatter_helper(self, group, group_id, rank):
        for dest in group:
            tensor = _build_tensor(dest + 1, -1)
            expected_tensor = _build_tensor(dest + 1, rank)
            tensors = (
                [_build_tensor(dest + 1, i) for i in group] if rank == dest else []
            )
            dist.scatter(tensor, src=dest, scatter_list=tensors, group=group_id)
            self.assertEqual(tensor, expected_tensor)

        self._barrier()

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_scatter_checks(self):
        group, group_id, rank = self._init_global_test()
        one = torch.ones([1])

        # Specify scatter_list argument only on source rank.
        output = one.clone() * -1
        if rank == 0:
            scatter_list = [one.clone() * i for i in group]
            dist.scatter(output, src=0, scatter_list=scatter_list)
        else:
            dist.scatter(output, src=0)
        self.assertEqual(output, one * rank)

        # Don't specify src argument.
        output = one.clone() * -1
        if rank == 0:
            scatter_list = [one.clone() * i for i in group]
            dist.scatter(output, scatter_list=scatter_list)
        else:
            dist.scatter(output)
        self.assertEqual(output, one * rank)

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support scatter")
    def test_scatter(self):
        group, group_id, rank = self._init_global_test()
        self._test_scatter_helper(group, group_id, rank)

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support scatter")
    @skip_if_small_worldsize
    def test_scatter_group(self):
        group, group_id, rank = self._init_group_test()
        self._test_scatter_helper(group, group_id, rank)

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support scatter")
    def test_scatter_full_group(self):
        group, group_id, rank = self._init_full_group_test()
        self._test_scatter_helper(group, group_id, rank)

    # GATHER
    def _test_gather_helper(self, group, group_id, rank):
        for dest in group:
            tensor = _build_tensor(dest + 1, rank)
            tensors = (
                [_build_tensor(dest + 1, -1) for i in group] if rank == dest else []
            )
            dist.gather(tensor, dst=dest, gather_list=tensors, group=group_id)
            if rank == dest:
                expected_tensors = [_build_tensor(dest + 1, i) for i in group]
                for t1, t2 in zip(tensors, expected_tensors):
                    self.assertEqual(t1, t2)

        self._barrier()

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_gather_checks(self):
        group, group_id, rank = self._init_global_test()
        one = torch.ones([1])

        # Specify gather_list argument only on destination rank.
        if rank == 0:
            gather_list = [one.clone() for _ in group]
            dist.gather(one * rank, dst=0, gather_list=gather_list)
            for i in group:
                self.assertEqual(gather_list[i], one * i)
        else:
            dist.gather(one * rank, dst=0)

        # Don't specify dst argument.
        if rank == 0:
            gather_list = [one.clone() for _ in group]
            dist.gather(one * rank, gather_list=gather_list)
            for i in group:
                self.assertEqual(gather_list[i], one * i)
        else:
            dist.gather(one * rank)

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_gather(self):
        group, group_id, rank = self._init_global_test()
        self._test_gather_helper(group, group_id, rank)

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    @skip_if_small_worldsize
    def test_gather_group(self):
        group, group_id, rank = self._init_group_test()
        self._test_gather_helper(group, group_id, rank)

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_gather_full_group(self):
        group, group_id, rank = self._init_full_group_test()
        self._test_gather_helper(group, group_id, rank)

    # ALL GATHER
    def _test_all_gather_helper(
        self, group, group_id, rank, cuda=False, rank_to_GPU=None
    ):
        for dest in group:
            tensor = _build_tensor(dest + 1, rank)
            tensors = [_build_tensor(dest + 1, -1) for i in group]
            if cuda:
                tensor = tensor.cuda(rank_to_GPU[rank][0])
                tensors = [t.cuda(rank_to_GPU[rank][0]) for t in tensors]
            dist.all_gather(tensors, tensor, group_id)

            expected_tensors = [_build_tensor(dest + 1, i) for i in group]
            for t1, t2 in zip(tensors, expected_tensors):
                self.assertEqual(t1, t2)

        self._barrier()

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_all_gather(self):
        group, group_id, rank = self._init_global_test()
        self._test_all_gather_helper(group, group_id, rank)

    @unittest.skipIf(BACKEND != "nccl", "Only Nccl supports CUDA all gather")
    @unittest.skipIf(BACKEND == "nccl", "CUDA all gather skipped for NCCL")
    @skip_if_no_gpu
    def test_all_gather_cuda(self):
        group, group_id, rank = self._init_global_test()
        rank_to_GPU = self._init_multigpu_helper()
        self._test_all_gather_helper(group, group_id, rank, True, rank_to_GPU)

    @skip_if_small_worldsize
    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_all_gather_group(self):
        group, group_id, rank = self._init_group_test()
        self._test_all_gather_helper(group, group_id, rank)

    @unittest.skipIf(BACKEND == "nccl", "Nccl does not support CPU tensors")
    def test_all_gather_full_group(self):
        group, group_id, rank = self._init_full_group_test()
        self._test_all_gather_helper(group, group_id, rank)

    def _run_all_gather_coalesced_and_verify(
        self, output_tensor_lists, input_tensors, expected_tensors, group_id
    ):
        """
        Helper that runs all_gather_coalesced and returns true if output
        matches expectations.
        """
        dist.all_gather_coalesced(
            output_tensor_lists, input_tensors, group_id)

        for l1, l2 in zip(output_tensor_lists, expected_tensors):
            for t1, t2 in zip(l1, l2):
                if not torch.equal(t1, t2):
                    return False
        return True

    def _test_all_gather_coalesced_helper(
        self, group, group_id, rank
    ):
        # TODO: Instead we should probably go through _rank_not_in_group
        # mechanism to disable sending tensors
        if group_id is not None:
            for test_case_id in range(2, 5):
                # Make sure we create tensors of incompatible sizes, e.g.
                # [1], [2x2], [3x3x3] ... to be sent in one batch
                input_tensors = [
                    _build_multidim_tensor(
                        tensor_id, tensor_id, rank + tensor_id) for tensor_id in range(
                            1, test_case_id)
                ]
                output_tensor_lists = [
                    [
                        _build_multidim_tensor(
                            tensor_id, tensor_id, -1) for tensor_id in range(
                                1, test_case_id)
                    ] for _ in group
                ]
                expected_tensors = [
                    [
                        _build_multidim_tensor(
                            tensor_id,
                            tensor_id,
                            rank_iter + tensor_id) for tensor_id in range(
                                1, test_case_id)
                    ] for rank_iter in group
                ]
                assert self._run_all_gather_coalesced_and_verify(
                    output_tensor_lists, input_tensors,
                    expected_tensors, group_id
                ), "output tensors do not match expected ouputs"

        self._barrier()

    @unittest.skipIf(BACKEND == "nccl", "all_gather_coalesced does not support NCCL")
    @unittest.skipIf(BACKEND == "mpi", "all_gather_coalesced does not support MPI")
    def test_all_gather_coalesced_simple(self):
        group, group_id, rank = self._init_global_test()
        self._test_all_gather_coalesced_helper(group, group_id, rank)

    @skip_if_small_worldsize
    @unittest.skipIf(BACKEND == "nccl", "all_gather_coalesced does not support NCCL")
    @unittest.skipIf(BACKEND == "mpi", "all_gather_coalesced does not support MPI")
    def test_all_gather_coalesced_group(self):
        group, group_id, rank = self._init_group_test()
        self._test_all_gather_coalesced_helper(group, group_id, rank)

    @unittest.skipIf(BACKEND == "nccl", "all_gather_coalesced does not support NCCL")
    @unittest.skipIf(BACKEND == "mpi", "all_gather_coalesced does not support MPI")
    def test_all_gather_coalesced_full_group(self):
        group, group_id, rank = self._init_full_group_test()
        self._test_all_gather_coalesced_helper(group, group_id, rank)

    @unittest.skipIf(BACKEND == "nccl", "all_gather_coalesced does not support NCCL")
    @unittest.skipIf(BACKEND == "mpi", "all_gather_coalesced does not support MPI")
    def test_all_gather_coalesced_with_empty(self):
        group, group_id, rank = self._init_global_test()
        input_tensors = [
            rank * torch.ones([2, 2]),
            torch.ones([0]),
            (rank + 1) * torch.ones([3, 3]),
            torch.ones([0]),
            torch.ones([0])
        ]
        output_tensors_lists = [
            [
                -1 * torch.ones([2, 2]),
                -1 * torch.ones([0]),
                -1 * torch.ones([3, 3]),
                -1 * torch.ones([0]),
                -1 * torch.ones([0])
            ] for _ in group
        ]
        expected_tensors = [
            [
                r * torch.ones([2, 2]),
                torch.ones([0]),
                (r + 1) * torch.ones([3, 3]),
                torch.ones([0]),
                torch.ones([0])
            ] for r in group
        ]
        assert self._run_all_gather_coalesced_and_verify(
            output_tensors_lists, input_tensors, expected_tensors, group_id)
        self._barrier()

    # AllToAll
    def _test_all_to_all_single_equal_split_helper(self, group, group_id, rank):
        if group_id is not None:
            size = len(group)
            in_tensor = torch.ones([size, size]) * rank
            expected_tensor = torch.cat([torch.ones([1, size]) * i for i in group])
            out_tensor = torch.ones([size, size]) * -1
            dist.all_to_all_single(out_tensor, in_tensor, group=group_id)
            self.assertEqual(out_tensor, expected_tensor)
        self._barrier()

    def _test_all_to_all_single_unequal_split_helper(self, group, group_id, rank):
        if group_id is not None:
            size = len(group)
            in_splits = [i + 1 for i in group]
            out_splits = [rank + 1 for _ in group]
            in_tensor = torch.ones([sum(in_splits), size]) * rank
            out_tensor = torch.ones([(rank + 1) * size, size])
            expected_tensor = torch.cat([torch.ones([rank + 1, size]) * i for i in group])
            dist.all_to_all_single(
                out_tensor, in_tensor, out_splits, in_splits, group=group_id)
            self.assertEqual(out_tensor, expected_tensor)
        self._barrier()

    def _test_all_to_all_helper(self, group, group_id, rank):
        if group_id is not None:
            size = len(group)
            in_splits = [i + 1 for i in group]
            in_tensors = [
                torch.ones([in_splits[i], size]) * rank for i, _ in enumerate(group)
            ]
            out_tensors = [torch.ones([(rank + 1), size]) for _ in group]
            expected_tensors = [torch.ones([rank + 1, size]) * i for i in group]
            dist.all_to_all(out_tensors, in_tensors, group=group_id)
            for t1, t2 in zip(out_tensors, expected_tensors):
                self.assertEqual(t1, t2)
        self._barrier()

    @unittest.skipIf(BACKEND != "mpi", "Only MPI supports all_to_all_single")
    def test_all_to_all_single_equal_split(self):
        group, group_id, rank = self._init_global_test()
        self._test_all_to_all_single_equal_split_helper(group, group_id, rank)

    @unittest.skipIf(BACKEND != "mpi", "Only MPI supports all_to_all_single")
    def test_all_to_all_single_unequal_split(self):
        group, group_id, rank = self._init_global_test()
        self._test_all_to_all_single_unequal_split_helper(group, group_id, rank)

    @unittest.skipIf(BACKEND != "mpi", "Only MPI supports all_to_all")
    def test_all_to_all(self):
        group, group_id, rank = self._init_global_test()
        self._test_all_to_all_helper(group, group_id, rank)

    @unittest.skipIf(BACKEND != "mpi", "Only MPI supports all_to_all_single")
    @skip_if_small_worldsize
    def test_all_to_all_single_equal_split_group(self):
        group, group_id, rank = self._init_group_test()
        self._test_all_to_all_single_equal_split_helper(group, group_id, rank)

    @unittest.skipIf(BACKEND != "mpi", "Only MPI supports all_to_all_single")
    @skip_if_small_worldsize
    def test_all_to_all_single_unequal_split_group(self):
        group, group_id, rank = self._init_group_test()
        self._test_all_to_all_single_unequal_split_helper(group, group_id, rank)

    @unittest.skipIf(BACKEND != "mpi", "Only MPI supports all_to_all")
    @skip_if_small_worldsize
    def test_all_to_all_group(self):
        group, group_id, rank = self._init_group_test()
        self._test_all_to_all_helper(group, group_id, rank)

    @unittest.skipIf(BACKEND != "mpi", "Only MPI supports all_to_all_single")
    def test_all_to_all_single_equal_split_full_group(self):
        group, group_id, rank = self._init_full_group_test()
        self._test_all_to_all_single_equal_split_helper(group, group_id, rank)

    @unittest.skipIf(BACKEND != "mpi", "Only MPI supports all_to_all_single")
    def test_all_to_all_single_unequal_split_full_group(self):
        group, group_id, rank = self._init_full_group_test()
        self._test_all_to_all_single_unequal_split_helper(group, group_id, rank)

    @unittest.skipIf(BACKEND != "mpi", "Only MPI supports all_to_all")
    def test_all_to_all_full_group(self):
        group, group_id, rank = self._init_full_group_test()
        self._test_all_to_all_helper(group, group_id, rank)

    # BARRIER
    def _test_barrier_helper(
            self, group, group_id, rank, cuda=False, rank_to_GPU=None):
        WAIT_TIME = 0.3  # seconds

        for dest in group:
            expected_time = torch.DoubleTensor(1).fill_(0.0)
            if cuda:
                expected_time = expected_time.cuda(rank_to_GPU[rank][0])
            if dest == rank:
                expected_time.fill_(time.time() + WAIT_TIME)
                dist.broadcast(expected_time, dest, group_id)
                time.sleep(WAIT_TIME + 0.1)  # sleep a little bit longer
                dist.barrier(group_id)
            else:
                dist.broadcast(expected_time, dest, group_id)
                dist.barrier(group_id)
                self.assertGreaterEqual(
                    float(time.time()),
                    float(expected_time[0]),
                    "destination rank: %d, my rank: %d" % (dest, rank) +
                    " (if you see this failure, please report in #14554)")

        # Use higher timeout for the instance where the test runs
        # against a subgroup and uses a CUDA tensor for expected time.
        # The CUDA initialization for the participating processes can
        # take long enough for the barrier timeout to trigger on the
        # process that doesn't participate in the group.
        self._barrier(timeout=20)

    @skip_if_no_gpu
    @unittest.skipIf(BACKEND == "mpi", "MPI doesn't supports GPU barrier")
    def test_barrier_cuda(self):
        group, group_id, rank = self._init_global_test()
        rank_to_GPU = self._init_multigpu_helper()
        self._test_barrier_helper(group, group_id, rank, True, rank_to_GPU)

    @skip_if_small_worldsize
    @skip_if_no_gpu
    @unittest.skipIf(BACKEND == "mpi", "MPI doesn't supports GPU barrier")
    @skip_if_rocm
    def test_barrier_group_cuda(self):
        group, group_id, rank = self._init_group_test()
        rank_to_GPU = self._init_multigpu_helper()
        self._test_barrier_helper(group, group_id, rank, True, rank_to_GPU)

    @skip_if_small_worldsize
    @skip_if_no_gpu
    @unittest.skipIf(BACKEND == "mpi", "MPI doesn't supports GPU barrier")
    def test_barrier_full_group_cuda(self):
        group, group_id, rank = self._init_full_group_test()
        rank_to_GPU = self._init_multigpu_helper()
        self._test_barrier_helper(group, group_id, rank, True, rank_to_GPU)

    @unittest.skipIf(BACKEND == "nccl", "NCCL does not support CPU barrier")
    def test_barrier(self):
        group, group_id, rank = self._init_global_test()
        self._test_barrier_helper(group, group_id, rank)

    @skip_if_small_worldsize
    @unittest.skipIf(BACKEND == "nccl", "NCCL does not support CPU barrier")
    def test_barrier_group(self):
        group, group_id, rank = self._init_group_test()
        self._test_barrier_helper(group, group_id, rank)

    @unittest.skipIf(BACKEND == "nccl", "NCCL does not support CPU barrier")
    def test_barrier_full_group(self):
        group, group_id, rank = self._init_full_group_test()
        self._test_barrier_helper(group, group_id, rank)

    def _test_broadcast_multigpu_helper(self, group, group_id, rank, rank_to_GPU):
        for src in group:
            expected_tensor = _build_tensor(src + 1)
            tensors = [
                _build_tensor(src + 1, -1).cuda(device=i) for i in rank_to_GPU[rank]
            ]
            if rank == src:
                tensors[0] = expected_tensor.cuda(device=rank_to_GPU[rank][0])

            dist.broadcast_multigpu(tensors, src, group_id)
            for tensor in tensors:
                self.assertEqual(tensor, expected_tensor)
        self._barrier()

    @unittest.skipIf(BACKEND == "mpi", "MPI doesn't support broadcast multigpu")
    @unittest.skipIf(BACKEND == "nccl", "NCCL broadcast multigpu skipped")
    @skip_if_no_gpu
    def test_broadcast_multigpu(self):
        group, group_id, rank = self._init_global_test()
        rank_to_GPU = self._init_multigpu_helper()
        self._test_broadcast_multigpu_helper(group, group_id, rank, rank_to_GPU)

    def _test_all_reduce_multigpu_helper(
        self,
        group,
        group_id,
        rank,
        rank_to_GPU,
        op,
        master_value,
        worker_value,
        expected_value,
    ):
        for src in group:
            if rank == src:
                tensors = [
                    _build_tensor(src + 1, master_value).cuda(device=i)
                    for i in rank_to_GPU[rank]
                ]
            else:
                tensors = [
                    _build_tensor(src + 1, worker_value).cuda(device=i)
                    for i in rank_to_GPU[rank]
                ]

            dist.all_reduce_multigpu(tensors, op, group_id)
            expected_tensor = _build_tensor(src + 1, expected_value)
            for tensor in tensors:
                self.assertEqual(tensor, expected_tensor)

        self._barrier()

    @unittest.skipIf(BACKEND == "mpi", "MPI doesn't support broadcast multigpu")
    @unittest.skipIf(BACKEND == "nccl", "CUDA all_reduce multigpu skipped for NCCL")
    @skip_if_no_gpu
    def test_all_reduce_multigpu(self):
        group, group_id, rank = self._init_global_test()
        rank_to_GPU = self._init_multigpu_helper()
        self._test_all_reduce_multigpu_helper(
            group,
            group_id,
            rank,
            rank_to_GPU,
            dist.ReduceOp.SUM,
            2,
            10,
            (2 + 10 * (len(group) - 1)) * len(rank_to_GPU[0]),
        )

    def _test_reduce_multigpu_helper(
        self,
        group,
        group_id,
        rank,
        rank_to_GPU,
        op,
        master_value,
        worker_value,
        expected_value,
    ):
        for src in group:
            if rank == src:
                tensors = [
                    _build_tensor(src + 1, master_value).cuda(device=i)
                    for i in rank_to_GPU[rank]
                ]
                dist.reduce_multigpu(tensors, src, op, group_id)
                expected_tensor = _build_tensor(src + 1, expected_value)
                self.assertEqual(tensors[0], expected_tensor)
            else:
                tensors = [
                    _build_tensor(src + 1, worker_value).cuda(device=i)
                    for i in rank_to_GPU[rank]
                ]
                dist.reduce_multigpu(tensors, src, op, group_id)

        self._barrier()

    @unittest.skipIf(BACKEND != "nccl", "Only Nccl backend supports reduce multigpu")
    @skip_if_no_gpu
    @skip_if_rocm
    def test_reduce_multigpu(self):
        group, group_id, rank = self._init_global_test()
        rank_to_GPU = self._init_multigpu_helper()
        self._test_reduce_multigpu_helper(
            group,
            group_id,
            rank,
            rank_to_GPU,
            dist.ReduceOp.SUM,
            2,
            10,
            (2 + 10 * (len(group) - 1)) * len(rank_to_GPU[0]),
        )

    def _test_all_gather_multigpu_helper(self, group, group_id, rank, rank_to_GPU):
        for dest in group:
            tensors = [
                _build_tensor(dest + 1).cuda(device=i) for i in rank_to_GPU[rank]
            ]

            # construct expected output along with
            # a place holder to receive all gather results
            output_tensors = []
            expected_output = []
            output_per_gpu = (
                [_build_tensor(dest + 1, -1)] * len(rank_to_GPU[0]) * len(group)
            )
            expected_per_gpu = (
                [_build_tensor(dest + 1)] * len(rank_to_GPU[0]) * len(group)
            )
            for gpu in rank_to_GPU[rank]:
                output_tensors.append([t.cuda(device=gpu) for t in output_per_gpu])
                expected_output.append([t.cuda(device=gpu) for t in expected_per_gpu])

            dist.all_gather_multigpu(output_tensors, tensors, group_id)
            self.assertEqual(output_tensors, expected_output)

        self._barrier()

    @unittest.skipIf(BACKEND != "nccl", "Only Nccl backend supports allgather multigpu")
    @skip_if_no_gpu
    def test_all_gather_multigpu(self):
        group, group_id, rank = self._init_global_test()
        rank_to_GPU = self._init_multigpu_helper()
        self._test_all_gather_multigpu_helper(group, group_id, rank, rank_to_GPU)

    def _model_step(self, model):
        for param in model.parameters():
            if param.grad is not None:
                with torch.no_grad():
                    param += param.grad
                param.grad = None

    def _prepare_dummy_data(self, local_bs):
        # global_bs for DDP should be divisible by WORLD_SIZE
        global_bs = int(WORLD_SIZE) * local_bs
        input_cpu = torch.randn(global_bs, 2)
        target = torch.randn(global_bs, 4)
        loss = nn.MSELoss()
        return global_bs, input_cpu, target, loss

    # END TO END TEST FOR DISTRIBUTEDDATAPARALLEL
    def _test_DDP_helper(self, model, input_var, target, loss, scale_factor=1.0):
        model.train()
        output = model(input_var)
        l = loss(output, target) * scale_factor
        l.backward()

    def _assert_equal_param(self, param_gpu, param_DDP):
        self.assertEqual(len(param_gpu), len(param_DDP))
        for p_gpu, p_DDP in zip(param_gpu, param_DDP):
            self.assertEqual(p_gpu, p_DDP)

    def _test_DDP_5iter(
        self, model_base, model_DDP, input, target, loss, local_bs, rank, batch_size, test_save, offset=None, world_size=0
    ):
        for idx in range(5):
            # single cpu/gpu training
            self._test_DDP_helper(model_base, input, target, loss)

            if offset is None:
                offset = rank * local_bs

            # DDP training, DDP scatters subsets of input_cpu to nodes/GPUs
            self._test_DDP_helper(
                model_DDP,
                input[offset: offset + local_bs],
                target[offset: offset + local_bs],
                loss,
                world_size * local_bs / batch_size if world_size != 0 else 1,
            )

            # Update weights and run a second iteration to shake out errors
            self._model_step(model_base)
            self._model_step(model_DDP)
            self._assert_equal_param(
                list(model_base.parameters()), list(model_DDP.module.parameters())
            )

            # Shuffle the input so that DDP input is different
            input = input[torch.randperm(batch_size)]

            # save the model in the middle and reload
            if test_save and idx == 2 and INIT_METHOD.startswith("file://"):
                with tempfile.NamedTemporaryFile() as tmp:
                    torch.save(model_DDP, tmp.name)
                    model_DDP = torch.load(tmp.name)

        with tempfile.TemporaryFile() as tmp_file:
            torch.save(model_DDP, tmp_file)
            tmp_file.seek(0)
            saved_model = torch.load(tmp_file)
        for k in model_DDP.state_dict():
            self.assertEqual(model_DDP.state_dict()[k],
                             saved_model.state_dict()[k])

    def _test_DistributedDataParallel(self, gpu_subset, rank, output_device=None):
        # Run a simple end to end DDP model, use result of single node model
        # as baseline

        # cpu training setup
        model = DDP_NET

        # single gpu training setup
        model_gpu = copy.deepcopy(model)
        model_gpu.cuda(gpu_subset[0])

        # DDP training setup
        model_DDP = copy.deepcopy(model)
        model_DDP.cuda(gpu_subset[0])
        model_DDP = nn.parallel.DistributedDataParallel(
            model_DDP, device_ids=gpu_subset
        )

        # test serializable/unserializable
        with tempfile.NamedTemporaryFile() as tmp:
            torch.save(model_DDP, tmp.name)
            model_DDP = torch.load(tmp.name)

        # dummy data initialization
        local_bs = len(gpu_subset)
        global_bs, input_cpu, target, loss = self._prepare_dummy_data(local_bs)

        # check two model parameters over 5 iterations
        self._test_DDP_5iter(
            model_gpu,
            model_DDP,
            input_cpu.cuda(gpu_subset[0]),
            target.cuda(gpu_subset[0]),
            loss,
            local_bs,
            rank,
            global_bs,
            True
        )
        self._barrier()

    @unittest.skipIf(
        BACKEND == "nccl", "nccl does not support DDP on CPU models"
    )
    def test_DistributedDataParallelCPU(self):
        # Run a simple end to end DDP-CPU model, use result of single node
        # model as baseline
        group, group_id, rank = self._init_global_test()

        # cpu training setup
        model_base = DDP_NET

        # DDP-CPU training setup
        model_DDP = copy.deepcopy(model_base)
        model_DDP = nn.parallel.DistributedDataParallelCPU(model_DDP)

        # dummy data initialization
        local_bs = 2
        global_bs, input_cpu, target, loss = self._prepare_dummy_data(local_bs)

        # check two model parameters over 5 iterations
        self._test_DDP_5iter(
            model_base, model_DDP, input_cpu, target, loss, local_bs, rank, global_bs, False
        )
        self._barrier()

    @unittest.skipIf(BACKEND != 'nccl' and BACKEND != 'gloo',
                     "Only Nccl & Gloo backend support DistributedDataParallel")
    def test_DistributedDataParallel_requires_grad(self):
        # a module without gradients shouldn't be accepted
        self.assertRaises(AssertionError, lambda: nn.parallel.DistributedDataParallel(nn.Module()))

    @unittest.skipIf(
        BACKEND != "nccl" and BACKEND != "gloo",
        "Only NCCL and GLOO backend support DistributedDataParallel",
    )
    @skip_if_lt_x_gpu(2)
    @skip_if_rocm
    def test_DistributedDataParallel_non_default_stream(self):
        stream = torch.cuda.Stream()
        rank = self.rank
        with torch.cuda.stream(stream):
            net = torch.nn.parallel.DistributedDataParallel(
                torch.nn.Linear(1, 1, bias=False).cuda(rank), device_ids=[rank]
            )
            for i in range(1000):
                # Clear gradients manually
                grad = net.module.weight.grad
                if grad is not None:
                    grad.detach_()
                    grad.zero_()
                # Forward + BW
                batch = torch.tensor([rank]).float().cuda(rank)
                loss = net(batch).sum()
                loss.backward()
                # For each worker, the gradient on the weight should be worker_rank.
                grad = net.module.weight.grad
                avg = grad.clone()
                # All-reducing the gradient averages should give us the gradient
                # average. If not, then one of the workers has not correctly
                # written back the averaged gradient before this all-reduce call.
                dist.all_reduce(avg)
                world_size = int(os.environ["WORLD_SIZE"])
                avg.div_(world_size)
                expected_grad = sum(i for i in range(world_size)) / world_size
                self.assertEqual(
                    avg[0, 0],
                    expected_grad,
                    msg=f"Expected gradient of {expected_grad} but got {avg} on rank {self.rank}",
                )

    @unittest.skipIf(BACKEND != 'nccl' and BACKEND != 'gloo',
                     "Only Nccl & Gloo backend support DistributedDataParallel")
    @skip_if_no_gpu
    @skip_if_rocm
    def test_DistributedDataParallel(self):
        group, group_id, rank = self._init_global_test()
        rank_to_GPU = self._init_multigpu_helper()
        gpus = list(rank_to_GPU[rank])
        self._test_DistributedDataParallel(gpu_subset=gpus, rank=rank)

        # test output_device
        self._test_DistributedDataParallel(gpu_subset=gpus, rank=rank, output_device=torch.device('cuda'))

        # test device_ids
        gpus = list(map(lambda i: torch.device('cuda:' + str(i)), gpus))
        self._test_DistributedDataParallel(gpu_subset=gpus, rank=rank, output_device=torch.device('cuda'))

    def _test_DistributedDataParallel_SyncBatchNorm(self, gpu_subset, rank, local_bs, global_bs, offset, output_device=None):
        # Run a simple end to end DDP model, use result of single node model
        # as baseline

        # cpu training setup
        model = BN_NET

        # single gpu training setup
        model_gpu = copy.deepcopy(model)
        model_gpu.cuda(gpu_subset[0])

        # DDP training setup
        model_DDP = nn.SyncBatchNorm.convert_sync_batchnorm(copy.deepcopy(model))
        model_DDP.cuda(gpu_subset[0])
        model_DDP = nn.parallel.DistributedDataParallel(
            model_DDP, device_ids=gpu_subset
        )

        # test serializable/unserializable
        with tempfile.NamedTemporaryFile() as tmp:
            torch.save(model_DDP, tmp.name)
            model_DDP = torch.load(tmp.name)

        # data initialization
        input_cpu = torch.randn(global_bs, 2)
        target = torch.randn(global_bs, 4)
        loss = nn.MSELoss()

        # check two model parameters over 5 iterations
        self._test_DDP_5iter(
            model_gpu,
            model_DDP,
            input_cpu.cuda(gpu_subset[0]),
            target.cuda(gpu_subset[0]),
            loss,
            local_bs,
            rank,
            global_bs,
            True,
            offset,
            int(WORLD_SIZE)
        )
        self._barrier()

    @unittest.skipIf(BACKEND != 'nccl' and BACKEND != 'gloo',
                     "Only Nccl & Gloo backend support DistributedDataParallel")
    @skip_if_no_gpu
    def test_DistributedDataParallel_SyncBatchNorm(self):
        group, group_id, rank = self._init_global_test()
        rank_to_GPU = self._init_multigpu_helper()
        # DDP does not support replicating BN layers within a process, hence
        # testing with one module replica per process
        gpus = [rank]

        num_processes = int(WORLD_SIZE)
        local_bs = 2
        bs_offset = int(rank * 2)
        global_bs = int(num_processes * 2)

        self._test_DistributedDataParallel_SyncBatchNorm(
            gpu_subset=gpus,
            rank=rank,
            local_bs=local_bs,
            global_bs=global_bs,
            offset=bs_offset)

        # test output_device
        self._test_DistributedDataParallel_SyncBatchNorm(
            gpu_subset=gpus,
            rank=rank,
            local_bs=local_bs,
            global_bs=global_bs,
            offset=bs_offset,
            output_device=torch.device('cuda'))

        # test device_ids
        gpus = list(map(lambda i: torch.device('cuda:' + str(i)), gpus))
        self._test_DistributedDataParallel_SyncBatchNorm(
            gpu_subset=gpus,
            rank=rank,
            local_bs=local_bs,
            global_bs=global_bs,
            offset=bs_offset,
            output_device=torch.device('cuda'))

    @unittest.skipIf(BACKEND != 'nccl' and BACKEND != 'gloo',
                     "Only Nccl & Gloo backend support DistributedDataParallel")
    @skip_if_no_gpu
    def test_DistributedDataParallel_SyncBatchNorm_2D_Input(self):
        group, group_id, rank = self._init_global_test()
        rank_to_GPU = self._init_multigpu_helper()
        # DDP does not support replicating BN layers within a process, hence
        # testing with one module replica per process
        gpus = [rank]

        model = nn.BatchNorm1d(2)

        # single gpu training setup
        model_gpu = copy.deepcopy(model)
        model_gpu.cuda(gpus[0])

        # DDP training setup
        model_DDP = nn.SyncBatchNorm.convert_sync_batchnorm(copy.deepcopy(model))
        model_DDP.cuda(gpus[0])
        model_DDP = nn.parallel.DistributedDataParallel(
            model_DDP, device_ids=gpus
        )

        local_bs = len(gpus) * 2
        global_bs = int(WORLD_SIZE) * local_bs
        input_cpu = torch.randn(global_bs, 2)
        target = torch.randn(global_bs, 2)
        loss = nn.MSELoss()

        # disabling cudnn.
        # SyncBatchNorm goes through native_batch_norm kernel, this avoids the
        # numerical issue created by the divergent code path.
        with torch.backends.cudnn.flags(False):
            # check two model parameters over 5 iterations
            self._test_DDP_5iter(
                model_gpu,
                model_DDP,
                input_cpu.cuda(gpus[0]),
                target.cuda(gpus[0]),
                loss,
                local_bs,
                rank,
                global_bs,
                True
            )
            self._barrier()

    @unittest.skipIf(BACKEND != 'nccl' and BACKEND != 'gloo',
                     "Only Nccl & Gloo backend support DistributedDataParallel")
    @skip_if_no_gpu
    @require_world_size(2)
    @skip_if_rocm
    def test_DistributedDataParallel_SyncBatchNorm_Single_Input_Per_Process(self):
        group, group_id, rank = self._init_global_test()
        rank_to_GPU = self._init_multigpu_helper()
        # DDP does not support replicating BN layers within a process, hence
        # testing with one module replica per process
        gpus = [rank]

        model = nn.BatchNorm1d(2)

        # single gpu training setup
        model_gpu = copy.deepcopy(model)
        model_gpu.cuda(gpus[0])

        # DDP training setup
        model_DDP = nn.SyncBatchNorm.convert_sync_batchnorm(copy.deepcopy(model))
        model_DDP.cuda(gpus[0])
        model_DDP = nn.parallel.DistributedDataParallel(
            model_DDP, device_ids=gpus
        )

        local_bs = 1
        global_bs = int(WORLD_SIZE)
        input_cpu = torch.randn(global_bs, 2)
        target = torch.randn(global_bs, 2)
        loss = nn.MSELoss()

        # disabling cudnn.
        # SyncBatchNorm goes through native_batch_norm kernel, this avoids the
        # numerical issue created by the divergent code path.
        with torch.backends.cudnn.flags(False):
            # check two model parameters over 5 iterations
            self._test_DDP_5iter(
                model_gpu,
                model_DDP,
                input_cpu.cuda(gpus[0]),
                target.cuda(gpus[0]),
                loss,
                local_bs,
                rank,
                global_bs,
                True
            )
            self._barrier()

    @unittest.skipIf(BACKEND != 'nccl' and BACKEND != 'gloo',
                     "Only Nccl & Gloo backend support DistributedDataParallel")
    @skip_if_no_gpu
    def test_DistributedDataParallel_SyncBatchNorm_Diff_Input_Sizes_Running_Value(self):
        group, group_id, rank = self._init_global_test()
        rank_to_GPU = self._init_multigpu_helper()
        model = nn.parallel.DistributedDataParallel(ONLY_SBN_NET.cuda(rank), device_ids=[rank])

        input_var = []
        for i in range(int(WORLD_SIZE)):
            input_var_rank = torch.cat([
                torch.ones(2, 1, 10 ** (i + 1)) * (0.1 ** (i - 1)),
                torch.ones(2, 1, 10 ** (i + 1)) * (0.3 ** (i - 1))
            ], dim=1)
            input_var.append(input_var_rank)

        all_input_var = torch.cat(
            [x.permute(1, 0, 2).contiguous().view(ONLY_SBN_NET.num_features, -1) for x in input_var],
            dim=1
        ).cuda(rank)

        for i in range(100):
            y = model(input_var[rank].cuda(rank))
            y.mean().backward()

        running_mean, running_var = model.module.running_mean, model.module.running_var
        torch.testing.assert_allclose(running_mean, all_input_var.mean(1))
        torch.testing.assert_allclose(running_var, all_input_var.var(1))

    @unittest.skipIf(BACKEND != 'nccl' and BACKEND != 'gloo',
                     "Only Nccl & Gloo backend support DistributedDataParallel")
    @skip_if_no_gpu
    def test_DistributedDataParallel_SyncBatchNorm_Diff_Input_Sizes_gradient(self):
        group, group_id, rank = self._init_global_test()
        # only do single GPU per process
        gpus = [rank]

        # cpu training setup
        model = BN_NET

        num_processes = int(WORLD_SIZE)
        local_bs = rank + 2
        bs_offset = int((rank + 3) * rank / 2)
        global_bs = int((num_processes + 3) * num_processes / 2)

        self._test_DistributedDataParallel_SyncBatchNorm(
            gpu_subset=gpus,
            rank=rank,
            local_bs=local_bs,
            global_bs=global_bs,
            offset=bs_offset)

    @skipIfNoTorchVision
    def test_SyncBatchNorm_process_group(self):
        # When adopting `convert_sync_batchnorm` to convert a `nn.modules`,
        # it need to recursively pass the `process_group` in the module when the `SyncBatchNorm`
        # is nested in a sub-module or sub-sub-module (e.g. resnet50 in torchvision.models).

        process_ids = 0
        process_group = torch.distributed.new_group([process_ids])
        res50_model = torchvision.models.resnet50()
        res50_model_sync = nn.SyncBatchNorm.convert_sync_batchnorm(copy.deepcopy(res50_model), process_group)
        process_group_sync = res50_model_sync.layer1[0].bn1.process_group
        self.assertEqual(process_group_sync, process_group)

if BACKEND == "gloo" or BACKEND == "nccl":
    WORLD_SIZE = os.environ["WORLD_SIZE"]

    class TestDistBackend(MultiProcessTestCase, _DistTestBase):

        # Needed since MultiProcessTestCase assumes a world_size of 4, but we
        # run these tests under other various world_sizes.
        @property
        def world_size(self):
            return os.environ["WORLD_SIZE"]

        @classmethod
        def setUpClass(cls):
            os.environ["MASTER_ADDR"] = str(MASTER_ADDR)
            os.environ["MASTER_PORT"] = str(MASTER_PORT)
            os.environ["WORLD_SIZE"] = str(WORLD_SIZE)
            super().setUpClass()

        def setUp(self):
            super().setUp()
            global INIT_METHOD
            # initialize Barrier.
            Barrier.init()
            # We rely on tearDown for deleting the temporary file
            # TODO: this temporary file should be deduped with the file_name
            # in MultiProcessTestCase as part of supporting spawn mode for these tests.
            # https://github.com/pytorch/pytorch/issues/36663
            self.temporary_file = None
            if INIT_METHOD.startswith("file://"):
                self.temporary_file = tempfile.NamedTemporaryFile(delete=False)
                INIT_METHOD = "file://{}".format(self.temporary_file.name)

            # TODO: enable spawn mode https://github.com/pytorch/pytorch/issues/36663
            self._fork_processes()

        def tearDown(self):
            super(MultiProcessTestCase, self).tearDown()
            super(TestDistBackend, self).tearDown()

            # Clean up temporary file if we used one.
            if self.temporary_file:
                try:
                    os.unlink(self.temporary_file.name)
                except OSError as err:
                    # ENOENT is OK because the test is supposed to clean it up.
                    if err.errno != errno.ENOENT:
                        raise

        @classmethod
        def _run(cls, rank, test_name, file_name):
            self = cls(test_name)
            self.rank = rank
            self.file_name = file_name
            try:
                dist.init_process_group(
                    init_method=INIT_METHOD,
                    backend=BACKEND,
                    world_size=int(WORLD_SIZE),
                    rank=self.rank
                )
            except RuntimeError as e:
                if "recompile" in e.args[0]:
                    sys.exit(TEST_SKIPS["backend_unavailable"].exit_code)

                raise

            # Execute barrier prior to running test to ensure that every process
            # has finished initialization and that the following test
            # immediately exiting due to a skip doesn't cause flakiness.
            self._barrier()

            # self.id() == e.g. '__main__.TestDistributed.test_get_rank'
            # We're retreiving a corresponding test and executing it.
            getattr(self, test_name)()
            self._barrier()
            dist.destroy_process_group()
            sys.exit(0)


elif BACKEND == "mpi":
    WORLD_SIZE = os.environ["WORLD_SIZE"]
    dist.init_process_group(init_method=INIT_METHOD, backend="mpi")

    class TestMPI(TestCase, _DistTestBase):
        pass

elif BACKEND == "test":
    class TestBackendDynamicLoad(TestCase):
        def setUp(self):
            super(TestBackendDynamicLoad, self).setUp()

        def _load_test_backend(self):
            temp_dir = tempfile.mkdtemp()
            src = "{}/../cpp_extensions/cpp_c10d_extension.cpp".format(os.path.abspath(os.path.dirname(__file__)))
            extension = torch.utils.cpp_extension.load(
                name="torch_test",
                sources=[src],
                build_directory=temp_dir
            )

        @skip_if_no_ninja
        def test_backend_apis(self):
            self._load_test_backend()

            os.environ['WORLD_SIZE'] = '1'
            os.environ['MASTER_ADDR'] = '127.0.0.1'
            os.environ['MASTER_PORT'] = str(find_free_port())
            os.environ['RANK'] = '0'

            dist.init_process_group(backend='test', init_method='env://', world_size=1, rank=0)
            self.assertEqual(dist.get_rank(), 0)
            self.assertEqual(dist.get_world_size(), 1)

            process_group = _get_default_group()
            work = process_group.allreduce([torch.rand(1), torch.rand(1)])
            self.assertTrue(work.wait())
            self.assertTrue(work.is_completed())
            self.assertTrue(work.is_success())

            work = process_group.broadcast([torch.rand(1)])
            self.assertTrue(work.wait())
            self.assertTrue(work.is_completed())
            self.assertTrue(work.is_success())

            dist.destroy_process_group()

if __name__ == "__main__":
    assert (
        not torch.cuda._initialized
    ), "test_distributed must not have initialized CUDA context on main process"

    run_tests()
