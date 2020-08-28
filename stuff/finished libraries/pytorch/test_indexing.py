from torch.testing._internal.common_utils import TestCase, run_tests
from torch.testing._internal.common_device_type import instantiate_device_type_tests, onlyCUDA, dtypes, dtypesIfCPU, dtypesIfCUDA
import torch
from torch import tensor
import unittest
import warnings

class TestIndexing(TestCase):
    def test_single_int(self, device):
        v = torch.randn(5, 7, 3, device=device)
        self.assertEqual(v[4].shape, (7, 3))

    def test_multiple_int(self, device):
        v = torch.randn(5, 7, 3, device=device)
        self.assertEqual(v[4].shape, (7, 3))
        self.assertEqual(v[4, :, 1].shape, (7,))

    def test_none(self, device):
        v = torch.randn(5, 7, 3, device=device)
        self.assertEqual(v[None].shape, (1, 5, 7, 3))
        self.assertEqual(v[:, None].shape, (5, 1, 7, 3))
        self.assertEqual(v[:, None, None].shape, (5, 1, 1, 7, 3))
        self.assertEqual(v[..., None].shape, (5, 7, 3, 1))

    def test_step(self, device):
        v = torch.arange(10, device=device)
        self.assertEqual(v[::1], v)
        self.assertEqual(v[::2].tolist(), [0, 2, 4, 6, 8])
        self.assertEqual(v[::3].tolist(), [0, 3, 6, 9])
        self.assertEqual(v[::11].tolist(), [0])
        self.assertEqual(v[1:6:2].tolist(), [1, 3, 5])

    def test_step_assignment(self, device):
        v = torch.zeros(4, 4, device=device)
        v[0, 1::2] = torch.tensor([3., 4.], device=device)
        self.assertEqual(v[0].tolist(), [0, 3, 0, 4])
        self.assertEqual(v[1:].sum(), 0)

    def test_bool_indices(self, device):
        v = torch.randn(5, 7, 3, device=device)
        boolIndices = torch.tensor([True, False, True, True, False], dtype=torch.bool, device=device)
        self.assertEqual(v[boolIndices].shape, (3, 7, 3))
        self.assertEqual(v[boolIndices], torch.stack([v[0], v[2], v[3]]))

        v = torch.tensor([True, False, True], dtype=torch.bool, device=device)
        boolIndices = torch.tensor([True, False, False], dtype=torch.bool, device=device)
        uint8Indices = torch.tensor([1, 0, 0], dtype=torch.uint8, device=device)
        with warnings.catch_warnings(record=True) as w:
            self.assertEqual(v[boolIndices].shape, v[uint8Indices].shape)
            self.assertEqual(v[boolIndices], v[uint8Indices])
            self.assertEqual(v[boolIndices], tensor([True], dtype=torch.bool, device=device))
            self.assertEquals(len(w), 2)

    def test_bool_indices_accumulate(self, device):
        mask = torch.zeros(size=(10, ), dtype=torch.bool, device=device)
        y = torch.ones(size=(10, 10), device=device)
        y.index_put_((mask, ), y[mask], accumulate=True)
        self.assertEqual(y, torch.ones(size=(10, 10), device=device))

    def test_multiple_bool_indices(self, device):
        v = torch.randn(5, 7, 3, device=device)
        # note: these broadcast together and are transposed to the first dim
        mask1 = torch.tensor([1, 0, 1, 1, 0], dtype=torch.bool, device=device)
        mask2 = torch.tensor([1, 1, 1], dtype=torch.bool, device=device)
        self.assertEqual(v[mask1, :, mask2].shape, (3, 7))

    def test_byte_mask(self, device):
        v = torch.randn(5, 7, 3, device=device)
        mask = torch.ByteTensor([1, 0, 1, 1, 0]).to(device)
        with warnings.catch_warnings(record=True) as w:
            self.assertEqual(v[mask].shape, (3, 7, 3))
            self.assertEqual(v[mask], torch.stack([v[0], v[2], v[3]]))
            self.assertEquals(len(w), 2)

        v = torch.tensor([1.], device=device)
        self.assertEqual(v[v == 0], torch.tensor([], device=device))

    def test_byte_mask_accumulate(self, device):
        mask = torch.zeros(size=(10, ), dtype=torch.uint8, device=device)
        y = torch.ones(size=(10, 10), device=device)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            y.index_put_((mask, ), y[mask], accumulate=True)
            self.assertEqual(y, torch.ones(size=(10, 10), device=device))
            self.assertEquals(len(w), 2)

    def test_index_put_accumulate_large_tensor(self, device):
        # This test is for tensors with number of elements >= INT_MAX (2^31 - 1).
        N = (1 << 31) + 5
        dt = torch.int8
        a = torch.ones(N, dtype=dt, device=device)
        indices = torch.LongTensor([0, 1, -2, -1])
        values = torch.tensor([10, 11, 12, 13], dtype=dt, device=device)

        a.index_put_((indices, ), values, accumulate=True)

        self.assertEqual(a[0], 11)
        self.assertEqual(a[1], 12)
        self.assertEqual(a[2], 1)
        self.assertEqual(a[-100], 1)
        self.assertEqual(a[-2], 13)
        self.assertEqual(a[-1], 14)

    def test_multiple_byte_mask(self, device):
        v = torch.randn(5, 7, 3, device=device)
        # note: these broadcast together and are transposed to the first dim
        mask1 = torch.ByteTensor([1, 0, 1, 1, 0]).to(device)
        mask2 = torch.ByteTensor([1, 1, 1]).to(device)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            self.assertEqual(v[mask1, :, mask2].shape, (3, 7))
            self.assertEquals(len(w), 2)

    def test_byte_mask2d(self, device):
        v = torch.randn(5, 7, 3, device=device)
        c = torch.randn(5, 7, device=device)
        num_ones = (c > 0).sum()
        r = v[c > 0]
        self.assertEqual(r.shape, (num_ones, 3))

    def test_int_indices(self, device):
        v = torch.randn(5, 7, 3, device=device)
        self.assertEqual(v[[0, 4, 2]].shape, (3, 7, 3))
        self.assertEqual(v[:, [0, 4, 2]].shape, (5, 3, 3))
        self.assertEqual(v[:, [[0, 1], [4, 3]]].shape, (5, 2, 2, 3))

    @dtypes(torch.float, torch.bfloat16, torch.long, torch.bool)
    @dtypesIfCPU(torch.float, torch.long, torch.bool, torch.bfloat16)
    @dtypesIfCUDA(torch.half, torch.long, torch.bool)
    def test_index_put_src_datatype(self, device, dtype):
        src = torch.ones(3, 2, 4, device=device, dtype=dtype)
        vals = torch.ones(3, 2, 4, device=device, dtype=dtype)
        indices = (torch.tensor([0, 2, 1]),)
        res = src.index_put_(indices, vals, accumulate=True)
        self.assertEqual(res.shape, src.shape)

    @dtypes(torch.float, torch.bfloat16, torch.long, torch.bool)
    @dtypesIfCPU(torch.float, torch.long, torch.bfloat16, torch.bool)
    @dtypesIfCUDA(torch.half, torch.long, torch.bfloat16, torch.bool)
    def test_index_src_datatype(self, device, dtype):
        src = torch.ones(3, 2, 4, device=device, dtype=dtype)
        # test index
        res = src[[0, 2, 1], :, :]
        self.assertEqual(res.shape, src.shape)
        # test index_put, no accum
        src[[0, 2, 1], :, :] = res
        self.assertEqual(res.shape, src.shape)

    def test_int_indices2d(self, device):
        # From the NumPy indexing example
        x = torch.arange(0, 12, device=device).view(4, 3)
        rows = torch.tensor([[0, 0], [3, 3]], device=device)
        columns = torch.tensor([[0, 2], [0, 2]], device=device)
        self.assertEqual(x[rows, columns].tolist(), [[0, 2], [9, 11]])

    def test_int_indices_broadcast(self, device):
        # From the NumPy indexing example
        x = torch.arange(0, 12, device=device).view(4, 3)
        rows = torch.tensor([0, 3], device=device)
        columns = torch.tensor([0, 2], device=device)
        result = x[rows[:, None], columns]
        self.assertEqual(result.tolist(), [[0, 2], [9, 11]])

    def test_empty_index(self, device):
        x = torch.arange(0, 12, device=device).view(4, 3)
        idx = torch.tensor([], dtype=torch.long, device=device)
        self.assertEqual(x[idx].numel(), 0)

        # empty assignment should have no effect but not throw an exception
        y = x.clone()
        y[idx] = -1
        self.assertEqual(x, y)

        mask = torch.zeros(4, 3, device=device).bool()
        y[mask] = -1
        self.assertEqual(x, y)

    def test_empty_ndim_index(self, device):
        x = torch.randn(5, device=device)
        self.assertEqual(torch.empty(0, 2, device=device), x[torch.empty(0, 2, dtype=torch.int64, device=device)])

        x = torch.randn(2, 3, 4, 5, device=device)
        self.assertEqual(torch.empty(2, 0, 6, 4, 5, device=device),
                         x[:, torch.empty(0, 6, dtype=torch.int64, device=device)])

        x = torch.empty(10, 0, device=device)
        self.assertEqual(x[[1, 2]].shape, (2, 0))
        self.assertEqual(x[[], []].shape, (0,))
        with self.assertRaisesRegex(IndexError, 'for dimension with size 0'):
            x[:, [0, 1]]

    def test_empty_ndim_index_bool(self, device):
        x = torch.randn(5, device=device)
        self.assertRaises(IndexError, lambda: x[torch.empty(0, 2, dtype=torch.uint8, device=device)])

    def test_empty_slice(self, device):
        x = torch.randn(2, 3, 4, 5, device=device)
        y = x[:, :, :, 1]
        z = y[:, 1:1, :]
        self.assertEqual((2, 0, 4), z.shape)
        # this isn't technically necessary, but matches NumPy stride calculations.
        self.assertEqual((60, 20, 5), z.stride())
        self.assertTrue(z.is_contiguous())

    def test_index_getitem_copy_bools_slices(self, device):
        true = torch.tensor(1, dtype=torch.uint8, device=device)
        false = torch.tensor(0, dtype=torch.uint8, device=device)

        tensors = [torch.randn(2, 3, device=device), torch.tensor(3, device=device)]

        for a in tensors:
            self.assertNotEqual(a.data_ptr(), a[True].data_ptr())
            # TODO(#38095): Replace assertEqualIgnoreType. See issue #38095
            self.assertEqualIgnoreType(torch.empty(0, *a.shape), a[False])
            self.assertNotEqual(a.data_ptr(), a[true].data_ptr())
            # TODO(#38095): Replace assertEqualIgnoreType. See issue #38095
            self.assertEqualIgnoreType(torch.empty(0, *a.shape), a[false])
            self.assertEqual(a.data_ptr(), a[None].data_ptr())
            self.assertEqual(a.data_ptr(), a[...].data_ptr())

    def test_index_setitem_bools_slices(self, device):
        true = torch.tensor(1, dtype=torch.uint8, device=device)
        false = torch.tensor(0, dtype=torch.uint8, device=device)

        tensors = [torch.randn(2, 3, device=device), torch.tensor(3, device=device)]

        for a in tensors:
            # prefix with a 1,1, to ensure we are compatible with numpy which cuts off prefix 1s
            # (some of these ops already prefix a 1 to the size)
            neg_ones = torch.ones_like(a) * -1
            neg_ones_expanded = neg_ones.unsqueeze(0).unsqueeze(0)
            a[True] = neg_ones_expanded
            self.assertEqual(a, neg_ones)
            a[False] = 5
            self.assertEqual(a, neg_ones)
            a[true] = neg_ones_expanded * 2
            self.assertEqual(a, neg_ones * 2)
            a[false] = 5
            self.assertEqual(a, neg_ones * 2)
            a[None] = neg_ones_expanded * 3
            self.assertEqual(a, neg_ones * 3)
            a[...] = neg_ones_expanded * 4
            self.assertEqual(a, neg_ones * 4)
            if a.dim() == 0:
                with self.assertRaises(IndexError):
                    a[:] = neg_ones_expanded * 5

    def test_index_scalar_with_bool_mask(self, device):
        a = torch.tensor(1, device=device)
        uintMask = torch.tensor(True, dtype=torch.uint8, device=device)
        boolMask = torch.tensor(True, dtype=torch.bool, device=device)
        self.assertEqual(a[uintMask], a[boolMask])
        self.assertEqual(a[uintMask].dtype, a[boolMask].dtype)

        a = torch.tensor(True, dtype=torch.bool, device=device)
        self.assertEqual(a[uintMask], a[boolMask])
        self.assertEqual(a[uintMask].dtype, a[boolMask].dtype)

    def test_setitem_expansion_error(self, device):
        true = torch.tensor(True, device=device)
        a = torch.randn(2, 3, device=device)
        # check prefix with  non-1s doesn't work
        a_expanded = a.expand(torch.Size([5, 1]) + a.size())
        # NumPy: ValueError
        with self.assertRaises(RuntimeError):
            a[True] = a_expanded
        with self.assertRaises(RuntimeError):
            a[true] = a_expanded

    def test_getitem_scalars(self, device):
        zero = torch.tensor(0, dtype=torch.int64, device=device)
        one = torch.tensor(1, dtype=torch.int64, device=device)

        # non-scalar indexed with scalars
        a = torch.randn(2, 3, device=device)
        self.assertEqual(a[0], a[zero])
        self.assertEqual(a[0][1], a[zero][one])
        self.assertEqual(a[0, 1], a[zero, one])
        self.assertEqual(a[0, one], a[zero, 1])

        # indexing by a scalar should slice (not copy)
        self.assertEqual(a[0, 1].data_ptr(), a[zero, one].data_ptr())
        self.assertEqual(a[1].data_ptr(), a[one.int()].data_ptr())
        self.assertEqual(a[1].data_ptr(), a[one.short()].data_ptr())

        # scalar indexed with scalar
        r = torch.randn((), device=device)
        with self.assertRaises(IndexError):
            r[:]
        with self.assertRaises(IndexError):
            r[zero]
        self.assertEqual(r, r[...])

    def test_setitem_scalars(self, device):
        zero = torch.tensor(0, dtype=torch.int64)

        # non-scalar indexed with scalars
        a = torch.randn(2, 3, device=device)
        a_set_with_number = a.clone()
        a_set_with_scalar = a.clone()
        b = torch.randn(3, device=device)

        a_set_with_number[0] = b
        a_set_with_scalar[zero] = b
        self.assertEqual(a_set_with_number, a_set_with_scalar)
        a[1, zero] = 7.7
        self.assertEqual(7.7, a[1, 0])

        # scalar indexed with scalars
        r = torch.randn((), device=device)
        with self.assertRaises(IndexError):
            r[:] = 8.8
        with self.assertRaises(IndexError):
            r[zero] = 8.8
        r[...] = 9.9
        self.assertEqual(9.9, r)

    def test_basic_advanced_combined(self, device):
        # From the NumPy indexing example
        x = torch.arange(0, 12, device=device).view(4, 3)
        self.assertEqual(x[1:2, 1:3], x[1:2, [1, 2]])
        self.assertEqual(x[1:2, 1:3].tolist(), [[4, 5]])

        # Check that it is a copy
        unmodified = x.clone()
        x[1:2, [1, 2]].zero_()
        self.assertEqual(x, unmodified)

        # But assignment should modify the original
        unmodified = x.clone()
        x[1:2, [1, 2]] = 0
        self.assertNotEqual(x, unmodified)

    def test_int_assignment(self, device):
        x = torch.arange(0, 4, device=device).view(2, 2)
        x[1] = 5
        self.assertEqual(x.tolist(), [[0, 1], [5, 5]])

        x = torch.arange(0, 4, device=device).view(2, 2)
        x[1] = torch.arange(5, 7, device=device)
        self.assertEqual(x.tolist(), [[0, 1], [5, 6]])

    def test_byte_tensor_assignment(self, device):
        x = torch.arange(0., 16, device=device).view(4, 4)
        b = torch.ByteTensor([True, False, True, False]).to(device)
        value = torch.tensor([3., 4., 5., 6.], device=device)

        with warnings.catch_warnings(record=True) as w:
            x[b] = value
            self.assertEquals(len(w), 1)

        self.assertEqual(x[0], value)
        # TODO(#38095): Replace assertEqualIgnoreType. See issue #38095
        self.assertEqualIgnoreType(x[1], torch.arange(4, 8, device=device))
        self.assertEqual(x[2], value)
        # TODO(#38095): Replace assertEqualIgnoreType. See issue #38095
        self.assertEqualIgnoreType(x[3], torch.arange(12, 16, device=device))

    def test_variable_slicing(self, device):
        x = torch.arange(0, 16, device=device).view(4, 4)
        indices = torch.IntTensor([0, 1]).to(device)
        i, j = indices
        self.assertEqual(x[i:j], x[0:1])

    def test_ellipsis_tensor(self, device):
        x = torch.arange(0, 9, device=device).view(3, 3)
        idx = torch.tensor([0, 2], device=device)
        self.assertEqual(x[..., idx].tolist(), [[0, 2],
                                                [3, 5],
                                                [6, 8]])
        self.assertEqual(x[idx, ...].tolist(), [[0, 1, 2],
                                                [6, 7, 8]])

    def test_invalid_index(self, device):
        x = torch.arange(0, 16, device=device).view(4, 4)
        self.assertRaisesRegex(TypeError, 'slice indices', lambda: x["0":"1"])

    def test_out_of_bound_index(self, device):
        x = torch.arange(0, 100, device=device).view(2, 5, 10)
        self.assertRaisesRegex(IndexError, 'index 5 is out of bounds for dimension 1 with size 5', lambda: x[0, 5])
        self.assertRaisesRegex(IndexError, 'index 4 is out of bounds for dimension 0 with size 2', lambda: x[4, 5])
        self.assertRaisesRegex(IndexError, 'index 15 is out of bounds for dimension 2 with size 10',
                               lambda: x[0, 1, 15])
        self.assertRaisesRegex(IndexError, 'index 12 is out of bounds for dimension 2 with size 10',
                               lambda: x[:, :, 12])

    def test_zero_dim_index(self, device):
        x = torch.tensor(10, device=device)
        self.assertEqual(x, x.item())

        def runner():
            print(x[0])
            return x[0]

        self.assertRaisesRegex(IndexError, 'invalid index', runner)

    @onlyCUDA
    def test_invalid_device(self, device):
        idx = torch.tensor([0, 1])
        b = torch.zeros(5, device=device)
        c = torch.tensor([1., 2.], device="cpu")

        for accumulate in [True, False]:
            self.assertRaises(RuntimeError, lambda: torch.index_put_(b, (idx,), c, accumulate=accumulate))


# The tests below are from NumPy test_indexing.py with some modifications to
# make them compatible with PyTorch. It's licensed under the BDS license below:
#
# Copyright (c) 2005-2017, NumPy Developers.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#
#     * Redistributions of source code must retain the above copyright
#        notice, this list of conditions and the following disclaimer.
#
#     * Redistributions in binary form must reproduce the above
#        copyright notice, this list of conditions and the following
#        disclaimer in the documentation and/or other materials provided
#        with the distribution.
#
#     * Neither the name of the NumPy Developers nor the names of any
#        contributors may be used to endorse or promote products derived
#        from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

class NumpyTests(TestCase):
    def test_index_no_floats(self, device):
        a = torch.tensor([[[5.]]], device=device)

        self.assertRaises(IndexError, lambda: a[0.0])
        self.assertRaises(IndexError, lambda: a[0, 0.0])
        self.assertRaises(IndexError, lambda: a[0.0, 0])
        self.assertRaises(IndexError, lambda: a[0.0, :])
        self.assertRaises(IndexError, lambda: a[:, 0.0])
        self.assertRaises(IndexError, lambda: a[:, 0.0, :])
        self.assertRaises(IndexError, lambda: a[0.0, :, :])
        self.assertRaises(IndexError, lambda: a[0, 0, 0.0])
        self.assertRaises(IndexError, lambda: a[0.0, 0, 0])
        self.assertRaises(IndexError, lambda: a[0, 0.0, 0])
        self.assertRaises(IndexError, lambda: a[-1.4])
        self.assertRaises(IndexError, lambda: a[0, -1.4])
        self.assertRaises(IndexError, lambda: a[-1.4, 0])
        self.assertRaises(IndexError, lambda: a[-1.4, :])
        self.assertRaises(IndexError, lambda: a[:, -1.4])
        self.assertRaises(IndexError, lambda: a[:, -1.4, :])
        self.assertRaises(IndexError, lambda: a[-1.4, :, :])
        self.assertRaises(IndexError, lambda: a[0, 0, -1.4])
        self.assertRaises(IndexError, lambda: a[-1.4, 0, 0])
        self.assertRaises(IndexError, lambda: a[0, -1.4, 0])
        # self.assertRaises(IndexError, lambda: a[0.0:, 0.0])
        # self.assertRaises(IndexError, lambda: a[0.0:, 0.0,:])

    def test_none_index(self, device):
        # `None` index adds newaxis
        a = tensor([1, 2, 3], device=device)
        self.assertEqual(a[None].dim(), a.dim() + 1)

    def test_empty_tuple_index(self, device):
        # Empty tuple index creates a view
        a = tensor([1, 2, 3], device=device)
        self.assertEqual(a[()], a)
        self.assertEqual(a[()].data_ptr(), a.data_ptr())

    def test_empty_fancy_index(self, device):
        # Empty list index creates an empty array
        a = tensor([1, 2, 3], device=device)
        # TODO(#38095): Replace assertEqualIgnoreType. See issue #38095
        self.assertEqualIgnoreType(a[[]], torch.tensor([], device=device))

        b = tensor([], device=device).long()
        self.assertEqual(a[[]], torch.tensor([], dtype=torch.long, device=device))

        b = tensor([], device=device).float()
        self.assertRaises(IndexError, lambda: a[b])

    def test_ellipsis_index(self, device):
        a = tensor([[1, 2, 3],
                    [4, 5, 6],
                    [7, 8, 9]], device=device)
        self.assertIsNot(a[...], a)
        self.assertEqual(a[...], a)
        # `a[...]` was `a` in numpy <1.9.
        self.assertEqual(a[...].data_ptr(), a.data_ptr())

        # Slicing with ellipsis can skip an
        # arbitrary number of dimensions
        self.assertEqual(a[0, ...], a[0])
        self.assertEqual(a[0, ...], a[0, :])
        self.assertEqual(a[..., 0], a[:, 0])

        # In NumPy, slicing with ellipsis results in a 0-dim array. In PyTorch
        # we don't have separate 0-dim arrays and scalars.
        self.assertEqual(a[0, ..., 1], torch.tensor(2, device=device))

        # Assignment with `(Ellipsis,)` on 0-d arrays
        b = torch.tensor(1)
        b[(Ellipsis,)] = 2
        self.assertEqual(b, 2)

    def test_single_int_index(self, device):
        # Single integer index selects one row
        a = tensor([[1, 2, 3],
                    [4, 5, 6],
                    [7, 8, 9]], device=device)

        self.assertEqual(a[0], [1, 2, 3])
        self.assertEqual(a[-1], [7, 8, 9])

        # Index out of bounds produces IndexError
        self.assertRaises(IndexError, a.__getitem__, 1 << 30)
        # Index overflow produces Exception  NB: different exception type
        self.assertRaises(Exception, a.__getitem__, 1 << 64)

    def test_single_bool_index(self, device):
        # Single boolean index
        a = tensor([[1, 2, 3],
                    [4, 5, 6],
                    [7, 8, 9]], device=device)

        self.assertEqual(a[True], a[None])
        self.assertEqual(a[False], a[None][0:0])

    def test_boolean_shape_mismatch(self, device):
        arr = torch.ones((5, 4, 3), device=device)

        index = tensor([True], device=device)
        self.assertRaisesRegex(IndexError, 'mask', lambda: arr[index])

        index = tensor([False] * 6, device=device)
        self.assertRaisesRegex(IndexError, 'mask', lambda: arr[index])

        index = torch.ByteTensor(4, 4).to(device).zero_()
        self.assertRaisesRegex(IndexError, 'mask', lambda: arr[index])
        self.assertRaisesRegex(IndexError, 'mask', lambda: arr[(slice(None), index)])

    def test_boolean_indexing_onedim(self, device):
        # Indexing a 2-dimensional array with
        # boolean array of length one
        a = tensor([[0., 0., 0.]], device=device)
        b = tensor([True], device=device)
        self.assertEqual(a[b], a)
        # boolean assignment
        a[b] = 1.
        self.assertEqual(a, tensor([[1., 1., 1.]], device=device))

    def test_boolean_assignment_value_mismatch(self, device):
        # A boolean assignment should fail when the shape of the values
        # cannot be broadcast to the subscription. (see also gh-3458)
        a = torch.arange(0, 4, device=device)

        def f(a, v):
            a[a > -1] = tensor(v).to(device)

        self.assertRaisesRegex(Exception, 'shape mismatch', f, a, [])
        self.assertRaisesRegex(Exception, 'shape mismatch', f, a, [1, 2, 3])
        self.assertRaisesRegex(Exception, 'shape mismatch', f, a[:1], [1, 2, 3])

    def test_boolean_indexing_twodim(self, device):
        # Indexing a 2-dimensional array with
        # 2-dimensional boolean array
        a = tensor([[1, 2, 3],
                    [4, 5, 6],
                    [7, 8, 9]], device=device)
        b = tensor([[True, False, True],
                    [False, True, False],
                    [True, False, True]], device=device)
        self.assertEqual(a[b], tensor([1, 3, 5, 7, 9], device=device))
        self.assertEqual(a[b[1]], tensor([[4, 5, 6]], device=device))
        self.assertEqual(a[b[0]], a[b[2]])

        # boolean assignment
        a[b] = 0
        self.assertEqual(a, tensor([[0, 2, 0],
                                    [4, 0, 6],
                                    [0, 8, 0]], device=device))

    def test_boolean_indexing_weirdness(self, device):
        # Weird boolean indexing things
        a = torch.ones((2, 3, 4), device=device)
        self.assertEqual((0, 2, 3, 4), a[False, True, ...].shape)
        self.assertEqual(torch.ones(1, 2, device=device), a[True, [0, 1], True, True, [1], [[2]]])
        self.assertRaises(IndexError, lambda: a[False, [0, 1], ...])

    def test_boolean_indexing_weirdness_tensors(self, device):
        # Weird boolean indexing things
        false = torch.tensor(False, device=device)
        true = torch.tensor(True, device=device)
        a = torch.ones((2, 3, 4), device=device)
        self.assertEqual((0, 2, 3, 4), a[False, True, ...].shape)
        self.assertEqual(torch.ones(1, 2, device=device), a[true, [0, 1], true, true, [1], [[2]]])
        self.assertRaises(IndexError, lambda: a[false, [0, 1], ...])

    def test_boolean_indexing_alldims(self, device):
        true = torch.tensor(True, device=device)
        a = torch.ones((2, 3), device=device)
        self.assertEqual((1, 2, 3), a[True, True].shape)
        self.assertEqual((1, 2, 3), a[true, true].shape)

    def test_boolean_list_indexing(self, device):
        # Indexing a 2-dimensional array with
        # boolean lists
        a = tensor([[1, 2, 3],
                    [4, 5, 6],
                    [7, 8, 9]], device=device)
        b = [True, False, False]
        c = [True, True, False]
        self.assertEqual(a[b], tensor([[1, 2, 3]], device=device))
        self.assertEqual(a[b, b], tensor([1], device=device))
        self.assertEqual(a[c], tensor([[1, 2, 3], [4, 5, 6]], device=device))
        self.assertEqual(a[c, c], tensor([1, 5], device=device))

    def test_everything_returns_views(self, device):
        # Before `...` would return a itself.
        a = tensor([5], device=device)

        self.assertIsNot(a, a[()])
        self.assertIsNot(a, a[...])
        self.assertIsNot(a, a[:])

    def test_broaderrors_indexing(self, device):
        a = torch.zeros(5, 5, device=device)
        self.assertRaisesRegex(IndexError, 'shape mismatch', a.__getitem__, ([0, 1], [0, 1, 2]))
        self.assertRaisesRegex(IndexError, 'shape mismatch', a.__setitem__, ([0, 1], [0, 1, 2]), 0)

    def test_trivial_fancy_out_of_bounds(self, device):
        a = torch.zeros(5, device=device)
        ind = torch.ones(20, dtype=torch.int64, device=device)
        if a.is_cuda:
            raise unittest.SkipTest('CUDA asserts instead of raising an exception')
        ind[-1] = 10
        self.assertRaises(IndexError, a.__getitem__, ind)
        self.assertRaises(IndexError, a.__setitem__, ind, 0)
        ind = torch.ones(20, dtype=torch.int64, device=device)
        ind[0] = 11
        self.assertRaises(IndexError, a.__getitem__, ind)
        self.assertRaises(IndexError, a.__setitem__, ind, 0)

    def test_index_is_larger(self, device):
        # Simple case of fancy index broadcasting of the index.
        a = torch.zeros((5, 5), device=device)
        a[[[0], [1], [2]], [0, 1, 2]] = tensor([2., 3., 4.], device=device)

        self.assertTrue((a[:3, :3] == tensor([2., 3., 4.], device=device)).all())

    def test_broadcast_subspace(self, device):
        a = torch.zeros((100, 100), device=device)
        v = torch.arange(0., 100, device=device)[:, None]
        b = torch.arange(99, -1, -1, device=device).long()
        a[b] = v
        expected = b.double().unsqueeze(1).expand(100, 100)
        # TODO(#38095): Replace assertEqualIgnoreType. See issue #38095
        self.assertEqualIgnoreType(a, expected)

instantiate_device_type_tests(TestIndexing, globals())
instantiate_device_type_tests(NumpyTests, globals())

if __name__ == '__main__':
    run_tests()
