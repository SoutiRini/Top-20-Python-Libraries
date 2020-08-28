import numpy as np
import math
import torch
import io
import unittest
from copy import deepcopy
from hypothesis import given
from hypothesis import strategies as st

from torch.testing._internal.common_utils import TestCase, TEST_WITH_ROCM
import torch.testing._internal.hypothesis_utils as hu

hu.assert_deadline_disabled()

import tempfile

class Foo(torch.nn.Module):
    def __init__(self):
        super(Foo, self).__init__()
        self.qscheme = torch.per_tensor_symmetric

def _calculate_dynamic_qparams(X, dtype, reduce_range=False):
    """Calculate the dynamic quantization parameters (scale, zero_point)
    according to the min and max element of the tensor"""
    if isinstance(X, torch.Tensor):
        X = X.numpy()
    if dtype == torch.qint8:
        if reduce_range:
            qmin, qmax = -64, 63
        else:
            qmin, qmax = -128, 127
    else:  # dtype == torch.quint8
        if reduce_range:
            qmin, qmax = 0, 127
        else:
            qmin, qmax = 0, 255

    min_val = X.min().astype(dtype=np.float32)
    max_val = X.max().astype(dtype=np.float32)
    min_val = min(0.0, min_val)
    max_val = max(0.0, max_val)
    scale = (np.float64(max_val) - min_val) / (qmax - qmin)
    if scale == 0.0 or math.isinf(1.0 / scale):
        scale = np.float64(0.1)
        zero_point = 0

    zero_point_from_min = qmin - min_val / float(scale)
    zero_point_from_max = qmax - max_val / float(scale)
    zero_point_from_min_error = abs(qmin) - abs(min_val / float(scale))
    zero_point_from_max_error = abs(qmax) - abs(max_val / float(scale))
    if zero_point_from_min_error < zero_point_from_max_error:
        initial_zero_point = zero_point_from_min
    else:
        initial_zero_point = zero_point_from_max
    nudged_zero_point = 0

    if initial_zero_point < qmin:
        nudged_zero_point = qmin
    elif initial_zero_point > qmax:
        nudged_zero_point = qmax
    else:
        nudged_zero_point = int(round(initial_zero_point))

    return [scale.astype(np.float32), int(nudged_zero_point)]

def get_supported_device_types():
    return ['cpu', 'cuda'] if torch.cuda.is_available() and not TEST_WITH_ROCM else ['cpu']

class TestQuantizedTensor(TestCase):
    def test_qtensor(self):
        num_elements = 10
        scale = 1.0
        zero_point = 2
        for device in get_supported_device_types():
            for dtype in [torch.qint8, torch.quint8, torch.qint32]:
                r = torch.ones(num_elements, dtype=torch.float, device=device)
                qr = torch.quantize_per_tensor(r, scale, zero_point, dtype)
                self.assertEqual(qr.q_scale(), scale)
                self.assertEqual(qr.q_zero_point(), zero_point)
                self.assertTrue(qr.is_quantized)
                self.assertFalse(r.is_quantized)
                self.assertEqual(qr.qscheme(), torch.per_tensor_affine)
                self.assertTrue(isinstance(qr.qscheme(), torch.qscheme))
                # slicing and int_repr
                int_repr = qr.int_repr()
                for num in int_repr:
                    self.assertEqual(num, 3)
                for num in qr[2:].int_repr():
                    self.assertEqual(num, 3)
                # dequantize
                rqr = qr.dequantize()
                for i in range(num_elements):
                    self.assertEqual(r[i], rqr[i])
                # we can also print a qtensor
                empty_r = torch.ones((0, 1), dtype=torch.float, device=device)
                empty_qr = torch.quantize_per_tensor(empty_r, scale, zero_point, dtype)

                device_msg = "" if device == 'cpu' else "device='" + device + ":0', "
                dtype_msg = str(dtype) + ", "
                self.assertEqual(' '.join(str(empty_qr).split()),
                                 "tensor([], " + device_msg + "size=(0, 1), dtype=" + dtype_msg +
                                 "quantization_scheme=torch.per_tensor_affine, " +
                                 "scale=1.0, zero_point=2)")

    def test_qtensor_float_assignment(self):
        # Scalar Tensor
        # item
        scale = 1.0
        zero_point = 2
        r = torch.ones(1, dtype=torch.float)
        for dtype in [torch.qint8, torch.quint8, torch.qint32]:
            qr = torch.quantize_per_tensor(r, scale, zero_point, dtype=dtype)
            self.assertEqual(qr.item(), 1)
            self.assertEqual(qr[0].item(), 1)
            # assignment
            self.assertTrue(qr[0].is_quantized)
            qr[0] = 11.3  # float assignment
            self.assertEqual(qr.item(), 11)
            x = torch.ones(1, dtype=torch.float) * 15.3
            # Copying from a float Tensor
            qr[:] = x
            self.assertEqual(qr.item(), 15)

            dtype_msg = str(dtype) + ", "
            self.assertEqual(' '.join(str(qr).split()),
                             "tensor([15.], size=(1,), dtype=" + dtype_msg +
                             "quantization_scheme=torch.per_tensor_affine, " +
                             "scale=1.0, zero_point=2)")

    def test_qtensor_quant_dequant(self):
        scale = 0.02
        zero_point = 2
        for device in get_supported_device_types():
            r = torch.rand(3, 2, dtype=torch.float, device=device) * 4 - 2
            for dtype in [torch.qint8, torch.quint8, torch.qint32]:
                qr = torch.quantize_per_tensor(r, scale, zero_point, dtype)
                rqr = qr.dequantize()
                self.assertTrue(np.allclose(r.cpu().numpy(), rqr.cpu().numpy(), atol=2 / scale))

    # legacy constructor/new doesn't support qtensors
    def test_qtensor_legacy_new_failure(self):
        r = torch.rand(3, 2, dtype=torch.float) * 4 - 2
        scale = 0.02
        zero_point = 2
        qr = torch.quantize_per_tensor(r, scale, zero_point, torch.quint8)
        self.assertRaises(RuntimeError, lambda: qr.new(device='cpu'))
        self.assertRaises(RuntimeError, lambda: qr.new(r.storage()))
        self.assertRaises(RuntimeError, lambda: qr.new(r))
        self.assertRaises(RuntimeError, lambda: qr.new(torch.Size([2, 3])))
        self.assertRaises(RuntimeError, lambda: qr.new([6]))

    def test_per_channel_qtensor_creation(self):
        numel = 10
        ch_axis = 0
        scales = torch.rand(numel)
        zero_points = torch.randint(0, 10, size=(numel,))
        for dtype in [torch.qint8, torch.quint8]:
            q = torch._empty_per_channel_affine_quantized(
                [numel], scales=scales, zero_points=zero_points, axis=ch_axis, dtype=dtype)
            # TODO(#38095): Replace assertEqualIgnoreType. See issue #38095
            self.assertEqualIgnoreType(scales, q.q_per_channel_scales())
            self.assertEqual(zero_points, q.q_per_channel_zero_points())
            self.assertEqual(ch_axis, q.q_per_channel_axis())

        # create Tensor from uint8_t Tensor, scales and zero_points
        int_tensor = torch.randint(0, 100, size=(numel,), dtype=torch.uint8)
        q = torch._make_per_channel_quantized_tensor(int_tensor, scales, zero_points, ch_axis)
        self.assertEqual(int_tensor, q.int_repr())
        # TODO(#38095): Replace assertEqualIgnoreType. See issue #38095
        self.assertEqualIgnoreType(scales, q.q_per_channel_scales())
        self.assertEqual(zero_points, q.q_per_channel_zero_points())
        self.assertEqual(ch_axis, q.q_per_channel_axis())

    def test_qtensor_creation(self):
        scale = 0.5
        zero_point = 10
        numel = 10
        for device in get_supported_device_types():
            q = torch._empty_affine_quantized([numel], scale=scale, zero_point=zero_point,
                                              device=device, dtype=torch.quint8)
            self.assertEqual(scale, q.q_scale())
            self.assertEqual(zero_point, q.q_zero_point())

            # create Tensor from uint8_t Tensor, scale and zero_point
            int_tensor = torch.randint(0, 100, size=(10,), device=device, dtype=torch.uint8)
            q = torch._make_per_tensor_quantized_tensor(int_tensor, scale, zero_point)
            self.assertEqual(int_tensor, q.int_repr())
            self.assertEqual(scale, q.q_scale())
            self.assertEqual(zero_point, q.q_zero_point())

            # create via empty_like
            q = torch._empty_affine_quantized([numel], scale=scale, zero_point=zero_point,
                                              device=device, dtype=torch.quint8)
            q_el = torch.empty_like(q)
            self.assertEqual(q.q_scale(), q_el.q_scale())
            self.assertEqual(q.q_zero_point(), q_el.q_zero_point())
            self.assertEqual(q.dtype, q_el.dtype)

            # create via empty_like but change the dtype (currently not supported)
            with self.assertRaises(RuntimeError):
                torch.empty_like(q, dtype=torch.qint8)

    def test_qtensor_dtypes(self):
        r = torch.rand(3, 2, dtype=torch.float) * 4 - 2
        scale = 0.2
        zero_point = 2
        qr = torch.quantize_per_tensor(r, scale, zero_point, torch.qint8)
        rqr = qr.dequantize()
        self.assertTrue(np.allclose(r.numpy(), rqr.numpy(), atol=2 / scale))
        qr = torch.quantize_per_tensor(r, scale, zero_point, torch.quint8)
        rqr = qr.dequantize()
        self.assertTrue(np.allclose(r.numpy(), rqr.numpy(), atol=2 / scale))
        qr = torch.quantize_per_tensor(r, scale, zero_point, torch.qint32)
        rqr = qr.dequantize()
        self.assertTrue(np.allclose(r.numpy(), rqr.numpy(), atol=2 / scale))

    def test_qtensor_quantize_per_channel(self):
        r = torch.rand(3, 2, dtype=torch.float) * 4 - 2
        scales = torch.tensor([0.2, 0.03], dtype=torch.double)
        zero_points = torch.tensor([5, 10], dtype=torch.long)
        axis = 1

        def quantize_c(data, scales, zero_points):
            res = torch.empty((3, 2))
            quant_min, quant_max = 0, 255
            for i in range(3):
                for j in range(2):
                    res[i][j] = np.clip(np.round(data[i][j] / scales[j]) + zero_points[j], quant_min, quant_max)
            return res
        qr = torch.quantize_per_channel(r, scales, zero_points, axis, torch.quint8)
        rqr = qr.dequantize()
        self.assertTrue(np.allclose(qr.int_repr(), quantize_c(r, scales, zero_points)))
        self.assertTrue(np.allclose(r.numpy(), rqr.numpy(), atol=2 / np.min(scales.numpy())))

    def test_qtensor_permute(self):
        scale = 0.02
        zero_point = 1
        for device in get_supported_device_types():
            r = torch.rand(10, 30, 2, 2, device=device, dtype=torch.float) * 4 - 2
            for dtype in [torch.qint8, torch.quint8, torch.qint32]:
                qr = torch.quantize_per_tensor(r, scale, zero_point, dtype=dtype)
                qr = qr.transpose(0, 1)
                rqr = qr.dequantize()
                # compare transpose + dequantized result with orignal transposed result
                self.assertTrue(np.allclose(r.cpu().numpy().transpose([1, 0, 2, 3]), rqr.cpu().numpy(), atol=2 / scale))

                qr = torch.quantize_per_tensor(r, scale, zero_point, dtype=dtype)
                qr1 = qr.permute([1, 0, 2, 3])
                qr2 = qr.transpose(0, 1)
                # compare int representation after transformations
                self.assertEqual(qr1.int_repr(), qr2.int_repr())
                self.assertEqual(qr1.q_scale(), qr2.q_scale())
                self.assertEqual(qr1.q_zero_point(), qr2.q_zero_point())
                # compare dequantized result
                self.assertEqual(qr1.dequantize(), qr2.dequantize())
                # compare permuted + dequantized result with original transposed result
                self.assertTrue(np.allclose(qr2.dequantize().cpu().numpy(),
                                            r.cpu().numpy().transpose([1, 0, 2, 3]), atol=2 / scale))
                # make permuted result contiguous
                self.assertEqual(qr2.contiguous().int_repr(), qr2.int_repr())

                # change memory format
                qlast = qr.contiguous(memory_format=torch.channels_last)
                self.assertEqual(qr.stride(), list(reversed(sorted(qr.stride()))))
                self.assertNotEqual(qlast.stride(), list(reversed(sorted(qlast.stride()))))
                self.assertEqual(qr.int_repr(), qlast.int_repr())
                self.assertEqual(qr.q_scale(), qlast.q_scale())
                self.assertEqual(qr.q_zero_point(), qlast.q_zero_point())
                self.assertEqual(qlast.dequantize(), qr.dequantize())

                # permuting larger tensors
                x = torch.randn(64, 64, device=device)
                qx = torch.quantize_per_tensor(x, 1.0, 0, dtype)
                # should work
                qx.permute([1, 0])

    def test_qtensor_per_channel_permute(self):
        r = torch.rand(20, 10, 2, 2, dtype=torch.float) * 4 - 2
        dtype = torch.qint8
        scales = torch.rand(10) * 0.02 + 0.01
        zero_points = torch.round(torch.rand(10) * 2 - 1).to(torch.long)
        qr = torch.quantize_per_channel(r, scales, zero_points, 1, dtype)

        # we can't reorder the axis
        with self.assertRaises(RuntimeError):
            qr.transpose(0, 1)

        # but we can change memory format
        qlast = qr.contiguous(memory_format=torch.channels_last)
        self.assertEqual(qr.stride(), list(reversed(sorted(qr.stride()))))
        self.assertNotEqual(qlast.stride(), list(reversed(sorted(qlast.stride()))))
        self.assertEqual(qr.int_repr(), qlast.int_repr())
        # TODO(#38095): Replace assertEqualIgnoreType. See issue #38095
        self.assertEqualIgnoreType(scales, qlast.q_per_channel_scales())
        self.assertEqual(zero_points, qlast.q_per_channel_zero_points())
        self.assertEqual(1, qlast.q_per_channel_axis())
        self.assertEqual(qlast.dequantize(), qr.dequantize())

    def test_qtensor_load_save(self):
        scale = 0.2
        zero_point = 10
        # storage is not accessible on the cuda right now
        device = "cpu"
        r = torch.rand(15, 2, dtype=torch.float32, device=device) * 2
        for dtype in [torch.qint8, torch.quint8, torch.qint32]:
            qr = torch.quantize_per_tensor(r, scale, zero_point, dtype=dtype)
            qrv = qr[:, 1]
            with tempfile.NamedTemporaryFile() as f:
                # Serializing and Deserializing Tensor
                torch.save((qr, qrv), f)
                f.seek(0)
                qr2, qrv2 = torch.load(f)
                self.assertEqual(qr, qr2)
                self.assertEqual(qrv, qrv2)
                self.assertEqual(qr2.storage().data_ptr(), qrv2.storage().data_ptr())

    def test_qtensor_per_channel_load_save(self):
        r = torch.rand(20, 10, dtype=torch.float) * 4 - 2
        scales = torch.rand(10, dtype=torch.double) * 0.02 + 0.01
        zero_points = torch.round(torch.rand(10) * 20 + 1).to(torch.long)
        # quint32, cuda is not supported yet
        for dtype in [torch.quint8, torch.qint8]:
            qr = torch.quantize_per_channel(r, scales, zero_points, 1, dtype)
            with tempfile.NamedTemporaryFile() as f:
                # Serializing and Deserializing Tensor
                torch.save(qr, f)
                f.seek(0)
                qr2 = torch.load(f)
                self.assertEqual(qr, qr2)

    def test_qtensor_copy(self):
        scale = 0.5
        zero_point = 10
        numel = 10
        for device in get_supported_device_types():
            for dtype in [torch.qint8, torch.quint8, torch.qint32]:
                # copy from same scale and zero_point
                q = torch._empty_affine_quantized([numel], scale=scale,
                                                  zero_point=zero_point, device=device, dtype=dtype)
                q2 = torch._empty_affine_quantized([numel], scale=scale,
                                                   zero_point=zero_point, device=device, dtype=dtype)
                q.copy_(q2)
                self.assertEqual(q.int_repr(), q2.int_repr())
                self.assertEqual(q.q_scale(), q2.q_scale())
                self.assertEqual(q.q_zero_point(), q2.q_zero_point())
                # copying from different scale and zero_point
                scale = 3.2
                zero_point = 5
                q = torch._empty_affine_quantized([numel], scale=scale,
                                                  zero_point=zero_point, device=device, dtype=dtype)
                # check original scale and zero_points are set correctly
                self.assertEqual(q.q_scale(), scale)
                self.assertEqual(q.q_zero_point(), zero_point)
                q.copy_(q2)
                # check scale and zero_points has been copied
                self.assertEqual(q, q2)
                # can't copy from quantized tensor to non-quantized tensor
                r = torch.empty([numel], dtype=torch.float)
                q = torch._empty_affine_quantized([numel], scale=scale, zero_point=zero_point, dtype=torch.quint8)
                with self.assertRaisesRegex(RuntimeError, "please use dequantize"):
                    r.copy_(q)

    def test_torch_qtensor_deepcopy(self):
        # cuda is not supported yet
        device = "cpu"
        q_int = torch.randint(0, 100, [3, 5], device=device, dtype=torch.uint8)
        scale, zero_point = 2.0, 3
        q = torch._make_per_tensor_quantized_tensor(q_int, scale=scale, zero_point=zero_point)
        qc = deepcopy(q)
        self.assertEqual(qc, q)

    def test_qtensor_clone(self):
        numel = 10
        scale = 0.5
        zero_point = 10
        for device in get_supported_device_types():
            for dtype in [torch.qint8, torch.quint8, torch.qint32]:
                q2 = torch._empty_affine_quantized([numel], scale=scale, zero_point=zero_point,
                                                   device=device, dtype=dtype)
                q = q2.clone()
                # Check to make sure the scale and zero_point has been copied.
                self.assertEqual(q, q2)

    def test_qtensor_view(self):
        scale, zero_point, dtype = 1.0, 2, torch.uint8
        for device in get_supported_device_types():
            q_int = torch.randint(0, 100, [1, 2, 3], device=device, dtype=dtype)
            q = torch._make_per_tensor_quantized_tensor(q_int, scale=scale, zero_point=zero_point)
            q2 = q.view(1, 3, 2)
            self.assertEqual(q.numel(), q2.numel())
            # testing -1
            self.assertEqual(q, q2.view(1, -1, 3))

            a_int = torch.randint(0, 100, [1, 2, 3, 4], device=device, dtype=dtype)
            a = torch._make_per_tensor_quantized_tensor(a_int, scale=scale, zero_point=zero_point)
            b = a.transpose(1, 2)  # swaps 2nd and 3rd dimension
            c = a.view(1, 3, 2, 4)  # does not change tensor layout in memory
            self.assertEqual(b.size(), c.size())
            self.assertEqual(b.q_scale(), c.q_scale())
            self.assertEqual(b.q_zero_point(), c.q_zero_point())
            self.assertNotEqual(b.stride(), c.stride())
            # size is the same but the underlying data is different
            self.assertNotEqual(b.int_repr(), c.int_repr())
            # torch.equal is not supported for the cuda backend
            if device == 'cpu':
                self.assertFalse(torch.equal(b, c))
            else:
                self.assertRaises(RuntimeError, lambda: torch.equal(b, c))

            # a case can't view non-contiguos Tensor
            a_int = torch.randint(0, 100, [1, 2, 3, 4], device=device, dtype=dtype)
            a = torch._make_per_tensor_quantized_tensor(a_int, scale=scale, zero_point=zero_point)
            b = a.transpose(1, 2)  # swaps 2nd and 3rd dimension
            err_str = "view size is not compatible with input tensor's size and stride*"
            with self.assertRaisesRegex(RuntimeError, err_str):
                b.view(1, 4, 2, 3)
            # view on contiguous tensor is fine
            b.contiguous().view(1, 4, 2, 3)

    def test_qtensor_resize(self):
        scale, zero_point, dtype = 1.0, 2, torch.uint8
        sizes1 = [1, 2, 3, 4]
        sizes2 = [1 * 2, 3 * 4]
        sizes3 = [1, 2 * 3, 4]
        sizes4 = [1 * 2 * 3 * 4]
        sizes5 = [1, 2, 1, 3, 1, 4]

        q1_int = torch.randint(0, 100, sizes1, dtype=dtype)
        q1 = torch._make_per_tensor_quantized_tensor(q1_int, scale=scale, zero_point=zero_point)
        q2 = q1.resize(*sizes2)
        q3 = q2.resize(*sizes3)
        q4 = q3.resize(*sizes4)
        q5 = q4.resize(*sizes5)

        self.assertEqual(q1.numel(), q2.numel())
        self.assertEqual(q1.numel(), q3.numel())
        self.assertEqual(q1.numel(), q4.numel())
        self.assertEqual(q1.numel(), q5.numel())

        # Compare original and post-transpose
        a_int = torch.randint(0, 100, sizes1, dtype=dtype)
        a = torch._make_per_tensor_quantized_tensor(a_int, scale=scale, zero_point=zero_point)
        b = a.transpose(1, 2)  # swaps 2nd and 3rd dimension
        c = b.resize(*sizes1)  # Change the sizes back to the original

        self.assertEqual(a.size(), c.size())
        self.assertEqual(b.q_scale(), c.q_scale())
        self.assertEqual(b.q_zero_point(), c.q_zero_point())
        self.assertNotEqual(b.stride(), c.stride())
        # size is the same but the underlying data is different
        self.assertNotEqual(b.int_repr(), c.int_repr())
        self.assertFalse(torch.equal(b, c))

        # Throws an error if numel is wrong
        q1_int = torch.randint(0, 100, sizes1, dtype=dtype)
        q1 = torch._make_per_tensor_quantized_tensor(a_int, scale=scale, zero_point=zero_point)
        err_str = "requested resize to*"
        with self.assertRaisesRegex(RuntimeError, err_str):
            q2 = q1.resize(*sizes1[:-1])
        # resize on both contiguous and non-contiguous tensor should be fine
        q3 = q1.resize(*sizes2)
        q4 = q1.contiguous().resize(*sizes2)

    def test_qtensor_reshape(self):
        scale, zero_point, dtype = 1.0, 2, torch.uint8
        for device in get_supported_device_types():
            q_int = torch.randint(0, 100, [3, 5], dtype=dtype, device=device)
            q = torch._make_per_tensor_quantized_tensor(q_int, scale=scale, zero_point=zero_point)
            q2 = q.reshape([15])
            self.assertEqual(q.numel(), q2.numel())
            self.assertEqual(q2.size(), [15])
            # testing -1
            self.assertEqual(q, q2.reshape([3, -1]))

            a_int = torch.randint(0, 100, [1, 2, 3, 4], dtype=dtype, device=device)
            a = torch._make_per_tensor_quantized_tensor(a_int, scale=scale, zero_point=zero_point)
            b = a.transpose(1, 2)  # swaps 2nd and 3rd dimension
            c = a.reshape(1, 3, 2, 4)  # does not change tensor layout
            self.assertEqual(b.size(), c.size())
            self.assertEqual(b.q_scale(), c.q_scale())
            self.assertEqual(b.q_zero_point(), c.q_zero_point())
            self.assertNotEqual(b.stride(), c.stride())
            self.assertNotEqual(b.int_repr(), c.int_repr())
            # torch.equal is not supported for the cuda backend
            if device == 'cpu':
                self.assertFalse(torch.equal(b, c))
            else:
                self.assertRaises(RuntimeError, lambda: torch.equal(b, c))

            # we can use reshape for non-contiguous Tensor
            a_int = torch.randint(0, 100, [1, 2, 3, 4], dtype=dtype, device=device)
            a = torch._make_per_tensor_quantized_tensor(a_int, scale=scale, zero_point=zero_point)
            b = a.transpose(1, 2)  # swaps 2nd and 3rd dimension
            c = b.reshape(1, 4, 2, 3)

    def test_qtensor_unsqueeze(self):
        x = torch.randn((1, 3, 4))
        qx = torch.quantize_per_tensor(x, scale=1.0, zero_point=0, dtype=torch.quint8)
        qy = qx.unsqueeze(2)
        self.assertEqual(qy.size(), (1, 3, 1, 4))
        qy = qy.squeeze(2)
        self.assertEqual(qy.size(), qx.size())

        # Per channel qtensor
        scales = torch.tensor([1.0])
        zero_points = torch.tensor([0])
        qx = torch.quantize_per_channel(x, scales=scales, zero_points=zero_points, dtype=torch.quint8, axis=0)
        qy = qx.unsqueeze(0)
        self.assertEqual(qy.size(), (1, 1, 3, 4))
        self.assertEqual(qy.q_per_channel_axis(), 1)

        qz = qy.squeeze(0)
        self.assertEqual(qz.size(), x.size())
        self.assertEqual(qz.q_per_channel_axis(), 0)
        with self.assertRaisesRegex(RuntimeError, "Squeeze is only possible on non-axis dimension for Per-Channel"):
            qz = qy.squeeze(1)

        # squeeze without dim specified
        x = torch.randn((3, 1, 2, 1, 4))
        scales = torch.tensor([1.0, 1.0])
        zero_points = torch.tensor([0, 0])
        qx = torch.quantize_per_channel(x, scales=scales, zero_points=zero_points, dtype=torch.quint8, axis=2)
        qz = qx.squeeze()
        self.assertEqual(qz.size(), (3, 2, 4))
        self.assertEqual(qz.q_per_channel_axis(), 1)
        with self.assertRaisesRegex(RuntimeError, "Squeeze is only possible on non-axis dimension for Per-Channel"):
            qz = qy.squeeze()

    def test_repeat(self):
        scale, zero_point, dtype = 1.0, 2, torch.uint8
        for device in get_supported_device_types():
            q_int = torch.randint(0, 100, [3], dtype=dtype, device=device)
            q_int_repeat = q_int.repeat(4, 2)
            q_ref = torch._make_per_tensor_quantized_tensor(q_int_repeat, scale=scale, zero_point=zero_point)

            q = torch._make_per_tensor_quantized_tensor(q_int, scale=scale, zero_point=zero_point)
            q_repeat = q.repeat(4, 2)
            self.assertEqual(q_ref, q_repeat)

    def test_qscheme_pickle(self):
        f = Foo()
        buf = io.BytesIO()
        torch.save(f, buf)

        buf.seek(0)
        f2 = torch.load(buf)

        self.assertEqual(f2.qscheme, torch.per_tensor_symmetric)

    @given(X=hu.tensor(shapes=hu.array_shapes(min_dims=2, max_dims=4,
                                              min_side=1, max_side=10),
                       qparams=hu.qparams()),
           reduce_range=st.booleans()
           )
    def test_choose_qparams(self, X, reduce_range):
        X, (scale, zero_point, torch_type) = X
        X = torch.from_numpy(X)
        X_scale, X_zp = _calculate_dynamic_qparams(X, torch.quint8, reduce_range=reduce_range)
        qparams = torch._choose_qparams_per_tensor(X, reduce_range)
        np.testing.assert_array_almost_equal(X_scale, qparams[0], decimal=3)
        self.assertEqual(X_zp, qparams[1])

    @unittest.skipIf(not torch.cuda.is_available() or TEST_WITH_ROCM, 'CUDA is not available')
    def test_cuda_cpu_implementation_consistency(self):
        numel, zero_point, scale = 100, 2, 0.02
        r = torch.rand(numel, dtype=torch.float32, device='cpu') * 25 - 4
        for dtype in [torch.qint8, torch.quint8, torch.qint32]:
            qr_cpu = torch.quantize_per_tensor(r, scale, zero_point, dtype=dtype)
            qr_cuda = torch.quantize_per_tensor(r.cuda(), scale, zero_point, dtype=dtype)
            # intr repr must be the same
            np.testing.assert_equal(qr_cpu.int_repr().numpy(), qr_cuda.int_repr().cpu().numpy())
            # dequantized values must be the same
            r_cpu, r_cuda = qr_cpu.dequantize().numpy(), qr_cuda.dequantize().cpu().numpy()
            np.testing.assert_almost_equal(r_cuda, r_cpu, decimal=5)

    @unittest.skipIf(not torch.cuda.is_available() or TEST_WITH_ROCM, 'CUDA is not available')
    def test_cuda_quantization_does_not_pin_memory(self):
        # Context - https://github.com/pytorch/pytorch/issues/41115
        x = torch.randn(3)
        self.assertEqual(x.is_pinned(), False)

        q_int = torch.randint(0, 100, [1, 2, 3], device="cuda", dtype=torch.uint8)
        q = torch._make_per_tensor_quantized_tensor(q_int, scale=0.1, zero_point=0)

        x = torch.randn(3)
        self.assertEqual(x.is_pinned(), False)
