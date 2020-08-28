import warnings

import torch._six
import torch.cuda


__all__ = ['all_reduce', 'reduce', 'broadcast', 'all_gather', 'reduce_scatter']

SUM = 0  # ncclRedOp_t


def is_available(tensors):
    if not hasattr(torch._C, '_nccl_all_reduce'):
        warnings.warn('PyTorch is not compiled with NCCL support')
        return False

    devices = set()
    for tensor in tensors:
        if tensor.is_sparse:
            return False
        if not tensor.is_contiguous():
            return False
        if not tensor.is_cuda:
            return False
        device = tensor.get_device()
        if device in devices:
            return False
        devices.add(device)

    return True


def version():
    return torch._C._nccl_version()


def unique_id():
    return torch._C._nccl_unique_id()


def init_rank(num_ranks, uid, rank):
    return torch._C._nccl_init_rank(num_ranks, uid, rank)


def all_reduce(inputs, outputs=None, op=SUM, streams=None, comms=None):
    if outputs is None:
        outputs = inputs
    torch._C._nccl_all_reduce(inputs, outputs, op, streams, comms)


# `output` used to be `outputs`, taking in a list of tensors. So we have two
# arguments for BC reasons.
def reduce(inputs, output=None, root=0, op=SUM, streams=None, comms=None, *, outputs=None):
    if outputs is not None:
        if output is not None:
            raise ValueError(
                "'output' and 'outputs' can not be both specified. 'outputs' is deprecated in "
                "favor of 'output', taking in a single output tensor. The signature of reduce is: "
                "reduce(inputs, output=None, root=0, op=SUM, streams=None, comms=None).")
        else:
            warnings.warn(
                "nccl.reduce with an output tensor list is deprecated. "
                "Please specify a single output tensor with argument 'output' instead instead.")
            output = outputs[root]
    elif not isinstance(output, torch.Tensor) and isinstance(output, torch._six.container_abcs.Sequence):
        # User called old API with positional arguments of list of output tensors.
        warnings.warn(
            "nccl.reduce with an output tensor list is deprecated. "
            "Please specify a single output tensor.")
        output = output[root]
    elif output is None:
        output = inputs[root]
    torch._C._nccl_reduce(inputs, output, root, op, streams, comms)


def broadcast(inputs, root=0, streams=None, comms=None):
    torch._C._nccl_broadcast(inputs, root, streams, comms)


def all_gather(inputs, outputs, streams=None, comms=None):
    torch._C._nccl_all_gather(inputs, outputs, streams, comms)


def reduce_scatter(inputs, outputs, op=SUM, streams=None, comms=None):
    torch._C._nccl_reduce_scatter(inputs, outputs, op, streams, comms)
